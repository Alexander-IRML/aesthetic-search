from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Protocol
import uuid

from artsearch.artwork_filter.errors import ArtworkFilterError
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate
from artsearch.bluesky.client import (
    BlueskyAPIError,
    BlueskyRequestBudget,
    BlueskyRequestBudgetExceeded,
)
from artsearch.bluesky_pipeline import BlueskyArtworkPipeline, PipelineSummary
from artsearch.discovery.config import DiscoveryConfig
from artsearch.discovery.models import (
    CampaignState,
    CreatorProfile,
    CreatorSamplingStatus,
    CreatorState,
    CreatorView,
    DiscoveryRequest,
    DiscoveryRunSummary,
    DiscoverySuggestion,
    ExpansionAction,
    FrontierStatus,
    FrontierTask,
    GraphEntityType,
    MediaBatchOutcome,
    SafetyState,
)
from artsearch.discovery.protocols import DiscoveryOracle, DiscoveryStore, Hydrator, MediaProcessor
from artsearch.discovery.repository import graph_entity_id
from artsearch.discovery.scoring import profile_safety_state, score_creator


LOGGER = logging.getLogger(__name__)


class NoveltyProvider(Protocol):
    async def score(
        self,
        profile: CreatorView,
        candidates: Sequence[ImageCandidate],
        results: Sequence[FilterResult],
    ) -> float: ...


class NeutralNoveltyProvider:
    """Explicit placeholder until accepted CLIP/CSD vectors exist for a creator."""

    async def score(
        self,
        profile: CreatorView,
        candidates: Sequence[ImageCandidate],
        results: Sequence[FilterResult],
    ) -> float:
        return 0.5


class ArtworkPipelineMediaProcessor:
    """Route sampled media through the same path used by normal corpus intake."""

    def __init__(self, pipeline: BlueskyArtworkPipeline) -> None:
        self.pipeline = pipeline
        self._model_lock = asyncio.Lock()

    async def process(self, candidates: Sequence[ImageCandidate]) -> MediaBatchOutcome:
        if not candidates:
            return MediaBatchOutcome(candidates=(), results=())
        summary = PipelineSummary()
        # The model and local SQLite corpus store have one owner per process.
        async with self._model_lock:
            outcome = await self.pipeline.process_candidates(list(candidates), summary=summary)
        return MediaBatchOutcome(
            candidates=tuple(candidates),
            results=tuple(outcome.results),
            pipeline_summary={
                key: int(value)
                for key, value in summary.to_dict().items()
                if isinstance(value, int)
            },
        )


@dataclass(frozen=True, slots=True)
class InitialDiscoverySource:
    oracle: DiscoveryOracle
    request: DiscoveryRequest
    name: str
    expand_without_sampling: bool = False


class DiscoveryCoordinator:
    """Run a bounded discovery campaign and feed sampled media into corpus intake."""

    def __init__(
        self,
        config: DiscoveryConfig,
        repository: DiscoveryStore,
        hydrator: Hydrator,
        media_processor: MediaProcessor,
        *,
        outbound_oracle: DiscoveryOracle | None = None,
        suggested_oracle: DiscoveryOracle | None = None,
        novelty_provider: NoveltyProvider | None = None,
        request_budget: BlueskyRequestBudget | None = None,
        worker_id: str = "artsearch-discovery",
    ) -> None:
        self.config = config
        self.repository = repository
        self.hydrator = hydrator
        self.media_processor = media_processor
        self.outbound_oracle = outbound_oracle
        self.suggested_oracle = suggested_oracle
        self.novelty_provider = novelty_provider or NeutralNoveltyProvider()
        self.request_budget = request_budget
        self.worker_id = worker_id
        self._profiles: dict[str, CreatorView] = {}
        self._source_reliability: dict[str, float] = {}
        self._seen_dids: set[str] = set()
        self._budget_lock = asyncio.Lock()
        self._expansion_locks: dict[str, asyncio.Lock] = {}
        self._fanout_limit_reported: set[str] = set()
        self._media_count = 0
        self._request_budget_exhausted = False

    async def run(
        self,
        campaign_name: str,
        initial_sources: Sequence[InitialDiscoverySource],
    ) -> DiscoveryRunSummary:
        summary = DiscoveryRunSummary()
        if self.request_budget is not None:
            summary.api_request_budget = self.request_budget.limit
        campaign_id = self.repository.create_campaign(campaign_name, self.config)
        summary.campaign_id = str(campaign_id)
        run_id = f"discovery:{campaign_id}"
        try:
            await self._run_initial_sources(campaign_id, run_id, initial_sources, summary)
            if not self._request_budget_exhausted:
                await self._drain_frontier(run_id, summary)
        except BaseException:
            if self.request_budget is not None:
                summary.api_requests = self.request_budget.used
            self.repository.finish_campaign(campaign_id, CampaignState.FAILED, summary)
            raise
        if self.request_budget is not None:
            summary.api_requests = self.request_budget.used
        self.repository.finish_campaign(campaign_id, CampaignState.COMPLETED, summary)
        return summary

    async def _run_initial_sources(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        sources: Sequence[InitialDiscoverySource],
        summary: DiscoveryRunSummary,
    ) -> None:
        semaphore = asyncio.Semaphore(self.config.crawl.concurrency)

        async def consume(source: InitialDiscoverySource) -> None:
            async with semaphore:
                try:
                    await self._consume_oracle(
                        campaign_id,
                        run_id,
                        source.oracle,
                        source.request,
                        summary,
                        expand_without_sampling=source.expand_without_sampling,
                    )
                except BlueskyRequestBudgetExceeded:
                    summary.budget_reached = 1
                    self._request_budget_exhausted = True
                except (BlueskyAPIError, OSError, RuntimeError, ValueError) as exc:
                    summary.source_errors += 1
                    LOGGER.warning("discovery.source_failed source=%s error=%s", source.name, exc)

        await asyncio.gather(*(consume(source) for source in sources))

    async def _consume_oracle(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        oracle: DiscoveryOracle,
        request: DiscoveryRequest,
        summary: DiscoveryRunSummary,
        *,
        parent_task_id: uuid.UUID | None = None,
        expand_without_sampling: bool = False,
    ) -> None:
        consumed = 0
        async for suggestion in oracle.suggest(request):
            if consumed >= request.limit:
                break
            consumed += 1
            await self._ingest_suggestion(
                campaign_id,
                run_id,
                suggestion,
                summary,
                parent_task_id=parent_task_id,
                expand_without_sampling=expand_without_sampling,
            )

    async def _ingest_suggestion(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        suggestion: DiscoverySuggestion,
        summary: DiscoveryRunSummary,
        *,
        parent_task_id: uuid.UUID | None,
        expand_without_sampling: bool = False,
    ) -> None:
        profile = suggestion.profile
        try:
            cached = self._profiles.get(
                profile.did if profile is not None else suggestion.actor_ref
            )
            profile = cached or await self.hydrator.creator(suggestion.actor_ref)
        except BlueskyRequestBudgetExceeded:
            raise
        except (BlueskyAPIError, KeyError, OSError, RuntimeError, ValueError) as exc:
            summary.source_errors += 1
            LOGGER.info(
                "discovery.creator_hydration_failed actor=%s error=%s",
                suggestion.actor_ref,
                exc,
            )
            return

        existing_sampling = self.repository.creator_sampling_status(profile.did)
        async with self._budget_lock:
            is_new_creator = profile.did not in self._seen_dids
            is_duplicate_account_id = not is_new_creator or existing_sampling is not None
            if is_new_creator and len(self._seen_dids) >= self.config.crawl.daily_creator_budget:
                summary.budget_reached = 1
                return
            if is_new_creator:
                self._seen_dids.add(profile.did)
                summary.unique_creators += 1
                summary.profiles_hydrated += 1
            if is_duplicate_account_id:
                summary.duplicate_creator_ids += 1
                if suggestion.graph_parent_type == GraphEntityType.CREATOR:
                    summary.duplicate_expansion_account_ids += 1

        if is_duplicate_account_id:
            LOGGER.info(
                "discovery.duplicate_creator_id did=%s parent=%s source=%s",
                profile.did,
                suggestion.graph_parent_ref or suggestion.source_ref,
                suggestion.source_type.value,
            )

        if self.repository.is_excluded("creator", profile.did, scope="discovery"):
            safety = SafetyState.BLOCKED
        else:
            safety = profile_safety_state(profile, self.config)
        inserted = self.repository.record_suggestion(
            campaign_id,
            run_id,
            suggestion,
            profile,
            safety_state=safety,
        )
        self.repository.hydrate_creator(
            campaign_id,
            run_id,
            profile,
            safety_state=safety,
        )
        self._profiles[profile.did] = profile
        self._source_reliability[profile.did] = max(
            suggestion.confidence,
            self._source_reliability.get(profile.did, 0.0),
        )
        if inserted:
            summary.observations += 1

        if safety == SafetyState.BLOCKED:
            self.repository.add_exclusion(
                subject_type="creator",
                subject_key=profile.did,
                reason_code="safety.profile_label",
                source="profile_moderation",
                reason="Profile carries a configured blocking label.",
            )
            summary.creators_blocked += 1
            return
        if suggestion.depth > self.config.crawl.max_depth:
            return
        if expand_without_sampling and safety == SafetyState.ALLOWED:
            self._enqueue_expansion_for_creator(
                campaign_id,
                did=profile.did,
                depth=suggestion.depth,
                priority=suggestion.confidence,
                parent_task_id=parent_task_id,
            )
            return
        sampling = existing_sampling
        if not self._should_sample(sampling):
            if sampling is not None and sampling.state == CreatorState.QUALIFIED:
                self._enqueue_expansion_for_creator(
                    campaign_id,
                    did=profile.did,
                    depth=suggestion.depth,
                    priority=sampling.expansion_score or suggestion.confidence,
                    parent_task_id=parent_task_id,
                )
            return
        creator_entity = graph_entity_id(GraphEntityType.CREATOR, profile.did)
        self.repository.enqueue_task(
            campaign_id,
            entity_id=creator_entity,
            action=ExpansionAction.SAMPLE_CREATOR_MEDIA,
            depth=suggestion.depth,
            priority=suggestion.confidence,
            budget_class="media",
            max_attempts=self.config.crawl.task_max_attempts,
            parent_task_id=parent_task_id,
            payload={
                "did": profile.did,
                "handle": profile.handle,
                "source_reliability": self._source_reliability[profile.did],
            },
        )

    async def _drain_frontier(
        self,
        run_id: str,
        summary: DiscoveryRunSummary,
    ) -> None:
        remaining = self.config.crawl.max_tasks_per_run
        while remaining > 0:
            batch_size = min(self.config.crawl.concurrency, remaining)
            tasks = self._claim_frontier_batch(batch_size)
            if not tasks:
                return
            summary.tasks_claimed += len(tasks)
            remaining -= len(tasks)
            await asyncio.gather(*(self._execute_task(task, run_id, summary) for task in tasks))
            if self._request_budget_exhausted:
                return
        summary.budget_reached = 1

    def _claim_frontier_batch(self, batch_size: int) -> list[FrontierTask]:
        exploration = int(batch_size * self.config.crawl.exploration_fraction)
        exploitation = batch_size - exploration
        tasks: list[FrontierTask] = []
        if exploitation:
            tasks.extend(
                self.repository.claim_tasks(
                    self.worker_id,
                    limit=exploitation,
                    lease_seconds=self.config.crawl.task_lease_seconds,
                    prefer_high_priority=True,
                )
            )
        if exploration:
            tasks.extend(
                self.repository.claim_tasks(
                    self.worker_id,
                    limit=exploration,
                    lease_seconds=self.config.crawl.task_lease_seconds,
                    prefer_high_priority=False,
                )
            )
        if len(tasks) < batch_size:
            tasks.extend(
                self.repository.claim_tasks(
                    self.worker_id,
                    limit=batch_size - len(tasks),
                    lease_seconds=self.config.crawl.task_lease_seconds,
                    prefer_high_priority=True,
                )
            )
        return tasks

    async def _execute_task(
        self,
        task: FrontierTask,
        run_id: str,
        summary: DiscoveryRunSummary,
    ) -> None:
        try:
            if task.action == ExpansionAction.SAMPLE_CREATOR_MEDIA:
                await self._sample_creator(task, task.campaign_id, run_id, summary)
            elif task.action == ExpansionAction.EXPAND_OUTBOUND_FOLLOWS:
                await self._expand_creator(
                    task,
                    task.campaign_id,
                    run_id,
                    summary,
                    oracle=self.outbound_oracle,
                    limit=self.config.crawl.max_outbound_follows_per_creator,
                )
            elif task.action == ExpansionAction.EXPAND_SUGGESTED_FOLLOWS:
                await self._expand_creator(
                    task,
                    task.campaign_id,
                    run_id,
                    summary,
                    oracle=self.suggested_oracle,
                    limit=self.config.crawl.max_suggested_follows_per_creator,
                )
            else:
                raise ValueError(f"unsupported frontier action: {task.action.value}")
        except BlueskyRequestBudgetExceeded as exc:
            status = self.repository.fail_task(
                task.task_id,
                self.worker_id,
                str(exc),
                retry_base_seconds=self.config.crawl.retry_base_seconds,
            )
            if status == FrontierStatus.DEAD:
                summary.tasks_dead += 1
            else:
                summary.tasks_retried += 1
            summary.budget_reached = 1
            self._request_budget_exhausted = True
            return
        except (
            ArtworkFilterError,
            BlueskyAPIError,
            KeyError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            status = self.repository.fail_task(
                task.task_id,
                self.worker_id,
                str(exc),
                retry_base_seconds=self.config.crawl.retry_base_seconds,
            )
            if status == FrontierStatus.DEAD:
                summary.tasks_dead += 1
            else:
                summary.tasks_retried += 1
            LOGGER.warning(
                "discovery.task_failed task=%s action=%s status=%s error=%s",
                task.task_id,
                task.action.value,
                status.value,
                exc,
            )
            return
        self.repository.complete_task(task.task_id, self.worker_id)
        summary.tasks_completed += 1

    async def _sample_creator(
        self,
        task: FrontierTask,
        campaign_id: uuid.UUID,
        run_id: str,
        summary: DiscoveryRunSummary,
    ) -> None:
        did = str(task.payload.get("did", ""))
        if not did:
            raise ValueError("sample task is missing creator DID")
        if self.repository.is_excluded("creator", did, scope="discovery"):
            return
        async with self._budget_lock:
            remaining = self.config.crawl.daily_media_budget - self._media_count
            if remaining <= 0:
                summary.budget_reached = 1
                return
            sample_limit = min(self.config.crawl.initial_media_sample, remaining)
        profile = self._profiles.get(did) or await self.hydrator.creator(did)
        candidates = await self.hydrator.media_candidates(did, limit=sample_limit)
        async with self._budget_lock:
            allowed = max(0, self.config.crawl.daily_media_budget - self._media_count)
            candidates = candidates[:allowed]
            self._media_count += len(candidates)
        outcome = await self.media_processor.process(candidates)
        self.repository.record_media_results(
            campaign_id,
            run_id,
            did,
            outcome.candidates,
            outcome.results,
        )
        _merge_pipeline_summary(summary, outcome.pipeline_summary)
        all_candidates = list(outcome.candidates)
        all_results = list(outcome.results)
        source_reliability = float(task.payload.get("source_reliability", 0.5))
        preliminary = score_creator(
            profile,
            all_candidates,
            all_results,
            self.config,
            source_reliability=source_reliability,
        )
        if (
            preliminary.state == CreatorState.QUALIFIED
            and self.config.crawl.deep_media_sample > self.config.crawl.initial_media_sample
        ):
            deep_pool = await self.hydrator.media_candidates(
                did,
                limit=self.config.crawl.deep_media_sample,
            )
            existing = {(item.candidate_id, item.post_cid) for item in all_candidates}
            deep_candidates = [
                item for item in deep_pool if (item.candidate_id, item.post_cid) not in existing
            ]
            async with self._budget_lock:
                allowed = max(0, self.config.crawl.daily_media_budget - self._media_count)
                deep_candidates = deep_candidates[:allowed]
                self._media_count += len(deep_candidates)
            if deep_candidates:
                deep_outcome = await self.media_processor.process(deep_candidates)
                self.repository.record_media_results(
                    campaign_id,
                    run_id,
                    did,
                    deep_outcome.candidates,
                    deep_outcome.results,
                )
                all_candidates.extend(deep_outcome.candidates)
                all_results.extend(deep_outcome.results)
                _merge_pipeline_summary(summary, deep_outcome.pipeline_summary)
                summary.creators_deepened += 1

        novelty = await self.novelty_provider.score(profile, all_candidates, all_results)
        creator_profile = score_creator(
            profile,
            all_candidates,
            all_results,
            self.config,
            source_reliability=source_reliability,
            novelty_score=novelty,
        )
        self.repository.record_creator_profile(
            campaign_id,
            run_id,
            creator_profile,
            config_hash=self.config.config_hash,
        )
        summary.creators_sampled += 1
        if creator_profile.state == CreatorState.QUALIFIED:
            summary.creators_qualified += 1
            self._enqueue_expansion_tasks(campaign_id, task, creator_profile)
        elif creator_profile.state == CreatorState.NEEDS_REVIEW:
            summary.creators_review += 1
        elif creator_profile.state == CreatorState.REJECTED:
            summary.creators_rejected += 1
        elif creator_profile.state == CreatorState.BLOCKED:
            summary.creators_blocked += 1

    async def _expand_creator(
        self,
        task: FrontierTask,
        campaign_id: uuid.UUID,
        run_id: str,
        summary: DiscoveryRunSummary,
        *,
        oracle: DiscoveryOracle | None,
        limit: int,
    ) -> None:
        if oracle is None or task.depth >= self.config.crawl.max_depth:
            return
        did = str(task.payload.get("did", ""))
        if not did:
            raise ValueError("creator expansion task is missing DID")
        if self.repository.is_excluded("creator", did, scope="discovery"):
            return
        lock = self._expansion_locks.setdefault(did, asyncio.Lock())
        async with lock:
            existing = self.repository.expansion_child_dids(did)
            fanout_limit = self.config.crawl.max_expansion_candidates_per_creator
            remaining = fanout_limit - len(existing)
            if remaining <= 0:
                self._record_fanout_limit(did, len(existing), summary)
                return
            await self._consume_oracle(
                campaign_id,
                run_id,
                oracle,
                DiscoveryRequest(
                    actor=did,
                    limit=min(limit, remaining),
                    depth=task.depth + 1,
                    campaign_id=campaign_id,
                ),
                summary,
                parent_task_id=task.task_id,
            )
            expanded = self.repository.expansion_child_dids(did)
            summary.expansion_candidates_added += len(expanded - existing)
            if len(expanded) >= fanout_limit:
                self._record_fanout_limit(did, len(expanded), summary)

    def _record_fanout_limit(
        self,
        did: str,
        child_count: int,
        summary: DiscoveryRunSummary,
    ) -> None:
        if did in self._fanout_limit_reported:
            return
        self._fanout_limit_reported.add(did)
        summary.expansion_nodes_at_limit += 1
        LOGGER.info(
            "discovery.creator_fanout_limit did=%s children=%s limit=%s",
            did,
            child_count,
            self.config.crawl.max_expansion_candidates_per_creator,
        )

    def _enqueue_expansion_tasks(
        self,
        campaign_id: uuid.UUID,
        parent: FrontierTask,
        profile: CreatorProfile,
    ) -> None:
        self._enqueue_expansion_for_creator(
            campaign_id,
            did=profile.did,
            depth=parent.depth,
            priority=profile.expansion_score,
            parent_task_id=parent.task_id,
        )

    def _enqueue_expansion_for_creator(
        self,
        campaign_id: uuid.UUID,
        *,
        did: str,
        depth: int,
        priority: float,
        parent_task_id: uuid.UUID | None,
    ) -> None:
        if depth >= self.config.crawl.max_depth:
            return
        entity_id = graph_entity_id(GraphEntityType.CREATOR, did)
        common = {
            "campaign_id": campaign_id,
            "entity_id": entity_id,
            "depth": depth,
            "priority": priority,
            "budget_class": "graph",
            "max_attempts": self.config.crawl.task_max_attempts,
            "parent_task_id": parent_task_id,
            "payload": {"did": did},
        }
        if self.config.sources.outbound_follows and self.outbound_oracle is not None:
            self.repository.enqueue_task(
                action=ExpansionAction.EXPAND_OUTBOUND_FOLLOWS,
                **common,
            )
        if self.config.sources.suggested_follows and self.suggested_oracle is not None:
            self.repository.enqueue_task(
                action=ExpansionAction.EXPAND_SUGGESTED_FOLLOWS,
                **common,
            )

    def _should_sample(self, status: CreatorSamplingStatus | None) -> bool:
        if status is None or status.media_sampled_at is None:
            return True
        if status.state in {CreatorState.BLOCKED, CreatorState.DELETED}:
            return False
        if status.score_version != self.config.scoring.version:
            return True
        sampled_at = status.media_sampled_at
        if sampled_at.tzinfo is None:
            sampled_at = sampled_at.replace(tzinfo=timezone.utc)
        interval_days = (
            self.config.qualification.reconsider_rejected_after_days
            if status.state == CreatorState.REJECTED
            else self.config.qualification.resample_after_days
        )
        return datetime.now(timezone.utc) - sampled_at >= timedelta(days=interval_days)


def _merge_pipeline_summary(
    discovery: DiscoveryRunSummary,
    pipeline: dict[str, int],
) -> None:
    for name in (
        "candidates",
        "accepted",
        "review",
        "rejected",
        "classification_errors",
        "routed",
        "imported",
        "duplicates",
        "review_stored",
        "route_errors",
    ):
        setattr(discovery, name, getattr(discovery, name) + int(pipeline.get(name, 0)))
