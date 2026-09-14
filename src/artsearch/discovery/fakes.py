from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
import uuid

from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate
from artsearch.discovery.config import DiscoveryConfig
from artsearch.discovery.models import (
    CampaignState,
    CreatorProfile,
    CreatorSamplingStatus,
    CreatorState,
    CreatorView,
    DiscoveryRunSummary,
    DiscoveryRequest,
    DiscoverySuggestion,
    ExpansionAction,
    FrontierStatus,
    FrontierTask,
    GraphEdgeType,
    GraphEntityType,
    MediaBatchOutcome,
    NetworkEvent,
    SafetyState,
)


class FakeDiscoveryOracle:
    def __init__(self, suggestions: Sequence[DiscoverySuggestion]) -> None:
        self.suggestions = tuple(suggestions)

    async def suggest(
        self,
        request: DiscoveryRequest,
    ) -> AsyncIterator[DiscoverySuggestion]:
        for suggestion in self.suggestions[: request.limit]:
            yield suggestion


class FakeNetworkStream:
    def __init__(self, events: Sequence[NetworkEvent]) -> None:
        self._events = tuple(events)

    async def events(self) -> AsyncIterator[NetworkEvent]:
        for event in self._events:
            yield event


class FakeHydrator:
    def __init__(
        self,
        profiles: Sequence[CreatorView],
        media: dict[str, Sequence[ImageCandidate]] | None = None,
    ) -> None:
        self.profiles = {
            identity: profile for profile in profiles for identity in (profile.did, profile.handle)
        }
        self.media = {key: tuple(values) for key, values in (media or {}).items()}

    async def creator(self, actor: str) -> CreatorView:
        try:
            return self.profiles[actor]
        except KeyError as exc:
            raise KeyError(f"unknown fake creator: {actor}") from exc

    async def media_candidates(self, did: str, *, limit: int) -> list[ImageCandidate]:
        return list(self.media.get(did, ()))[:limit]


class FakeMediaProcessor:
    def __init__(self, outcomes: dict[str, MediaBatchOutcome]) -> None:
        self.outcomes = outcomes

    async def process(self, candidates: Sequence[ImageCandidate]) -> MediaBatchOutcome:
        if not candidates:
            return MediaBatchOutcome(candidates=(), results=())
        did = candidates[0].author_did or ""
        return self.outcomes[did]


class InMemoryDiscoveryStore:
    """Deterministic control-plane fake for graph and frontier integration tests."""

    def __init__(self) -> None:
        self.campaigns: dict[uuid.UUID, dict[str, Any]] = {}
        self.creators: dict[str, CreatorView] = {}
        self.creator_states: dict[str, str] = {}
        self.observations: list[tuple[uuid.UUID, DiscoverySuggestion, str]] = []
        self.profiles: dict[str, CreatorProfile] = {}
        self.sampled_at: dict[str, datetime] = {}
        self.media_results: list[tuple[str, tuple[FilterResult, ...]]] = []
        self.tasks: dict[uuid.UUID, FrontierTask] = {}
        self.task_keys: set[tuple[uuid.UUID, str, ExpansionAction]] = set()
        self.leases: dict[uuid.UUID, str] = {}
        self.exclusions: set[tuple[str, str, str]] = set()
        self._observation_keys: set[tuple[object, ...]] = set()
        self._counter = 0

    def create_campaign(self, name: str, config: DiscoveryConfig) -> uuid.UUID:
        campaign_id = self._uuid()
        self.campaigns[campaign_id] = {
            "name": name,
            "state": CampaignState.ACTIVE,
            "config_hash": config.config_hash,
            "summary": None,
        }
        return campaign_id

    def finish_campaign(
        self,
        campaign_id: uuid.UUID,
        state: CampaignState,
        summary: DiscoveryRunSummary,
    ) -> None:
        self.campaigns[campaign_id]["state"] = state
        self.campaigns[campaign_id]["summary"] = summary.to_dict()

    def record_suggestion(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        suggestion: DiscoverySuggestion,
        profile: CreatorView,
        *,
        safety_state: SafetyState,
    ) -> bool:
        del run_id
        self.creators[profile.did] = profile
        self.creator_states.setdefault(profile.did, "discovered")
        key = (
            campaign_id,
            profile.did,
            suggestion.source_type,
            suggestion.source_ref,
            suggestion.graph_parent_id,
            suggestion.graph_edge_type,
            suggestion.query,
        )
        if key in self._observation_keys:
            return False
        self._observation_keys.add(key)
        self.observations.append((campaign_id, suggestion, profile.did))
        return True

    def hydrate_creator(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        profile: CreatorView,
        *,
        safety_state: SafetyState,
    ) -> None:
        del campaign_id, run_id
        self.creators[profile.did] = profile
        previous = self.creator_states.get(profile.did)
        if safety_state == SafetyState.BLOCKED:
            self.creator_states[profile.did] = "blocked"
        elif previous not in {
            "qualified",
            "needs_review",
            "deferred",
            "rejected",
            "blocked",
            "deleted",
        }:
            self.creator_states[profile.did] = "hydrated"

    def creator_sampling_status(self, did: str) -> CreatorSamplingStatus | None:
        state = self.creator_states.get(did)
        if state is None:
            return None
        profile = self.profiles.get(did)
        return CreatorSamplingStatus(
            state=CreatorState(state),
            safety_state=profile.safety_state if profile else SafetyState.ALLOWED,
            score_version=profile.score_version if profile else None,
            expansion_score=profile.expansion_score if profile else None,
            media_sampled_at=self.sampled_at.get(did),
        )

    def expansion_child_dids(self, creator_did: str) -> set[str]:
        expansion_edges = {
            GraphEdgeType.FOLLOWS,
            GraphEdgeType.SUGGESTED_FOLLOW,
            GraphEdgeType.GRAPH_SIMILAR,
        }
        return {
            did
            for _, suggestion, did in self.observations
            if suggestion.graph_parent_type == GraphEntityType.CREATOR
            and suggestion.graph_parent_ref == creator_did
            and suggestion.graph_edge_type in expansion_edges
            and did != creator_did
        }

    def record_media_results(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        creator_did: str,
        candidates: Sequence[ImageCandidate],
        results: Sequence[FilterResult],
    ) -> None:
        del campaign_id, run_id, candidates
        self.media_results.append((creator_did, tuple(results)))
        self.creator_states[creator_did] = "sampled"
        self.sampled_at[creator_did] = datetime.now(timezone.utc)

    def record_creator_profile(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        profile: CreatorProfile,
        *,
        config_hash: str,
    ) -> None:
        del campaign_id, run_id, config_hash
        self.profiles[profile.did] = profile
        self.creator_states[profile.did] = profile.state.value

    def enqueue_task(
        self,
        campaign_id: uuid.UUID,
        *,
        entity_id: str,
        action: ExpansionAction,
        depth: int,
        priority: float,
        budget_class: str,
        max_attempts: int,
        parent_task_id: uuid.UUID | None = None,
        payload: dict[str, object] | None = None,
    ) -> bool:
        key = (campaign_id, entity_id, action)
        if key in self.task_keys:
            return False
        did = str((payload or {}).get("did", ""))
        if did and self.is_excluded("creator", did, scope="discovery"):
            return False
        if any(
            task.entity_id == entity_id
            and task.action == action
            and task.status in {FrontierStatus.PENDING, FrontierStatus.RETRY, FrontierStatus.LEASED}
            for task in self.tasks.values()
        ):
            return False
        self.task_keys.add(key)
        task_id = self._uuid()
        self.tasks[task_id] = FrontierTask(
            task_id=task_id,
            campaign_id=campaign_id,
            entity_id=entity_id,
            action=action,
            status=FrontierStatus.PENDING,
            depth=depth,
            priority=priority,
            budget_class=budget_class,
            attempts=0,
            max_attempts=max_attempts,
            run_after=datetime.now(timezone.utc),
            parent_task_id=parent_task_id,
            payload=dict(payload or {}),
        )
        return True

    def claim_tasks(
        self,
        worker_id: str,
        *,
        limit: int,
        lease_seconds: int,
        campaign_id: uuid.UUID | None = None,
        prefer_high_priority: bool = True,
    ) -> list[FrontierTask]:
        del lease_seconds
        eligible = [
            task
            for task in self.tasks.values()
            if task.status in {FrontierStatus.PENDING, FrontierStatus.RETRY}
            and (campaign_id is None or task.campaign_id == campaign_id)
        ]
        direction = -1.0 if prefer_high_priority else 1.0
        eligible.sort(
            key=lambda task: (
                direction * task.priority,
                task.run_after,
                str(task.task_id),
            )
        )
        claimed = []
        for task in eligible[:limit]:
            leased = replace(
                task,
                status=FrontierStatus.LEASED,
                attempts=task.attempts + 1,
            )
            self.tasks[task.task_id] = leased
            self.leases[task.task_id] = worker_id
            claimed.append(leased)
        return claimed

    def complete_task(self, task_id: uuid.UUID, worker_id: str) -> bool:
        if self.leases.get(task_id) != worker_id:
            return False
        self.tasks[task_id] = replace(self.tasks[task_id], status=FrontierStatus.COMPLETED)
        self.leases.pop(task_id, None)
        return True

    def fail_task(
        self,
        task_id: uuid.UUID,
        worker_id: str,
        error: str,
        *,
        retry_base_seconds: int,
    ) -> FrontierStatus:
        del error, retry_base_seconds
        if self.leases.get(task_id) != worker_id:
            raise ValueError("frontier task is not leased by this worker")
        task = self.tasks[task_id]
        status = FrontierStatus.DEAD if task.attempts >= task.max_attempts else FrontierStatus.RETRY
        self.tasks[task_id] = replace(task, status=status)
        self.leases.pop(task_id, None)
        return status

    def is_excluded(self, subject_type: str, subject_key: str, *, scope: str) -> bool:
        return (subject_type, subject_key, scope) in self.exclusions or (
            subject_type,
            subject_key,
            "all",
        ) in self.exclusions

    def add_exclusion(
        self,
        *,
        subject_type: str,
        subject_key: str,
        reason_code: str,
        source: str,
        scope: str = "all",
        reason: str = "",
        created_by: str | None = None,
    ) -> None:
        del reason_code, source, reason, created_by
        self.exclusions.add((subject_type, subject_key, scope))

    def _uuid(self) -> uuid.UUID:
        self._counter += 1
        return uuid.uuid5(uuid.NAMESPACE_URL, f"artsearch-discovery-fake:{self._counter}")
