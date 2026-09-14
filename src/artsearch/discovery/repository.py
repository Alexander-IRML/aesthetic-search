from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any
import uuid

from sqlalchemy import Engine, and_, case, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate
from artsearch.discovery.config import DiscoveryConfig
from artsearch.discovery.models import (
    CampaignState,
    CreatorProfile,
    CreatorSamplingStatus,
    CreatorState,
    CreatorView,
    DiscoveryRunSummary,
    DiscoverySuggestion,
    ExpansionAction,
    FrontierStatus,
    FrontierTask,
    GraphEdgeType,
    GraphEntityType,
    MediaState,
    ReviewDecision,
    SafetyState,
)
from artsearch.postgres.ids import uuid7
from artsearch.postgres.schema import (
    discovery_campaigns,
    discovery_coverage_snapshots,
    discovery_creator_profiles,
    discovery_creators,
    discovery_edge_evidence,
    discovery_edges,
    discovery_entities,
    discovery_evidence,
    discovery_exclusions,
    discovery_frontier_tasks,
    discovery_media,
    discovery_observations,
    discovery_pipeline_events,
    discovery_posts,
    discovery_removal_requests,
    discovery_sources,
    outbox_events,
)


def graph_entity_id(entity_type: GraphEntityType, canonical_ref: str) -> str:
    normalized = canonical_ref.strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"{entity_type.value}:{digest}"


def stable_key(namespace: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{namespace}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


class DiscoveryRepository:
    """Canonical PostgreSQL store for discovery graph, evidence, and work leases."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create_campaign(self, name: str, config: DiscoveryConfig) -> uuid.UUID:
        campaign_id = uuid7()
        with self.engine.begin() as connection:
            connection.execute(
                insert(discovery_campaigns).values(
                    campaign_id=campaign_id,
                    name=name.strip() or "corpus-discovery",
                    config_version=config.version,
                    config_hash=config.config_hash,
                    budgets={
                        "daily_creator_budget": config.crawl.daily_creator_budget,
                        "daily_media_budget": config.crawl.daily_media_budget,
                        "max_depth": config.crawl.max_depth,
                        "max_expansion_candidates_per_creator": (
                            config.crawl.max_expansion_candidates_per_creator
                        ),
                        "max_tasks_per_run": config.crawl.max_tasks_per_run,
                        "max_api_requests_per_run": config.crawl.max_api_requests_per_run,
                    },
                )
            )
        return campaign_id

    def finish_campaign(
        self,
        campaign_id: uuid.UUID,
        state: CampaignState,
        summary: DiscoveryRunSummary,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(discovery_campaigns)
                .where(discovery_campaigns.c.campaign_id == campaign_id)
                .values(
                    state=state.value,
                    metrics=summary.to_dict(),
                    finished_at=func.now(),
                )
            )
        if state == CampaignState.COMPLETED:
            self.record_coverage_snapshot(campaign_id)

    def record_suggestion(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        suggestion: DiscoverySuggestion,
        profile: CreatorView,
        *,
        safety_state: SafetyState,
    ) -> bool:
        """Upsert one creator while preserving each independent discovery observation."""

        if suggestion.depth < 0:
            raise ValueError("discovery depth must be non-negative")
        source_id = uuid7()
        observation_id = uuid7()
        creator_entity_id = graph_entity_id(GraphEntityType.CREATOR, profile.did)
        source_entity_type = _source_entity_type(suggestion)
        source_entity_id = suggestion.graph_parent_id or graph_entity_id(
            source_entity_type,
            suggestion.source_ref,
        )
        observation_key = stable_key(
            "discovery-observation-v1",
            {
                "campaign_id": campaign_id,
                "candidate_did": profile.did,
                "source_type": suggestion.source_type.value,
                "source_ref": suggestion.source_ref,
                "parent": source_entity_id,
                "edge": suggestion.graph_edge_type.value
                if suggestion.graph_edge_type is not None
                else None,
                "query": suggestion.query,
            },
        )
        source_statement = (
            postgresql_insert(discovery_sources)
            .values(
                source_id=source_id,
                campaign_id=campaign_id,
                source_type=suggestion.source_type.value,
                source_ref=suggestion.source_ref,
                query=suggestion.query,
                priority=suggestion.confidence,
            )
            .on_conflict_do_update(
                constraint="uq_discovery_source_ref",
                set_={"last_seen_at": func.now(), "priority": suggestion.confidence},
            )
            .returning(discovery_sources.c.source_id)
        )
        with self.engine.begin() as connection:
            existing_creator = connection.execute(
                select(discovery_creators.c.did).where(discovery_creators.c.did == profile.did)
            ).scalar_one_or_none()
            resolved_source_id = connection.execute(source_statement).scalar_one()
            self._upsert_graph_entity(
                connection,
                source_entity_id,
                source_entity_type,
                suggestion.graph_parent_ref or suggestion.source_ref,
                suggestion.depth,
                suggestion.metadata,
            )
            self._upsert_graph_entity(
                connection,
                creator_entity_id,
                GraphEntityType.CREATOR,
                profile.did,
                suggestion.depth,
                {"handle": profile.handle},
            )
            self._upsert_creator(
                connection,
                profile,
                depth=suggestion.depth,
                safety_state=safety_state,
            )
            inserted = connection.execute(
                postgresql_insert(discovery_observations)
                .values(
                    observation_id=observation_id,
                    campaign_id=campaign_id,
                    source_id=resolved_source_id,
                    candidate_did=profile.did,
                    source_type=suggestion.source_type.value,
                    source_ref=suggestion.source_ref,
                    graph_parent_id=source_entity_id,
                    graph_edge_type=(
                        suggestion.graph_edge_type.value
                        if suggestion.graph_edge_type is not None
                        else None
                    ),
                    query=suggestion.query,
                    crawl_depth=suggestion.depth,
                    confidence=suggestion.confidence,
                    run_id=run_id,
                    idempotency_key=observation_key,
                    metadata=suggestion.metadata,
                )
                .on_conflict_do_nothing(index_elements=[discovery_observations.c.idempotency_key])
                .returning(discovery_observations.c.observation_id)
            ).scalar_one_or_none()
            if inserted is None:
                return False
            connection.execute(
                update(discovery_creators)
                .where(discovery_creators.c.did == profile.did)
                .values(
                    discovery_count=discovery_creators.c.discovery_count + 1,
                    last_seen_at=func.now(),
                )
            )
            if source_entity_id != creator_entity_id:
                self._record_edge(
                    connection,
                    source_entity_id=source_entity_id,
                    target_entity_id=creator_entity_id,
                    edge_type=suggestion.graph_edge_type or GraphEdgeType.SEARCH_HIT,
                    confidence=suggestion.confidence,
                    observation_id=inserted,
                    provenance_source=suggestion.source_type.value,
                    metadata=suggestion.metadata,
                )
            if existing_creator is None:
                self._append_event(
                    connection,
                    campaign_id=campaign_id,
                    entity_type=GraphEntityType.CREATOR,
                    entity_id=profile.did,
                    from_state=None,
                    to_state=CreatorState.DISCOVERED.value,
                    reason=f"discovered.{suggestion.source_type.value}",
                    run_id=run_id,
                )
        return True

    def hydrate_creator(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        profile: CreatorView,
        *,
        safety_state: SafetyState,
    ) -> None:
        with self.engine.begin() as connection:
            previous = connection.execute(
                select(discovery_creators.c.state).where(discovery_creators.c.did == profile.did)
            ).scalar_one_or_none()
            self._upsert_creator(connection, profile, depth=0, safety_state=safety_state)
            if previous == CreatorState.DELETED.value:
                target = CreatorState.DELETED
            elif previous == CreatorState.BLOCKED.value or safety_state == SafetyState.BLOCKED:
                target = CreatorState.BLOCKED
            elif previous in {
                CreatorState.QUALIFIED.value,
                CreatorState.NEEDS_REVIEW.value,
                CreatorState.DEFERRED.value,
                CreatorState.REJECTED.value,
                CreatorState.BLOCKED.value,
                CreatorState.DELETED.value,
            }:
                target = CreatorState(previous)
            else:
                target = CreatorState.HYDRATED
            connection.execute(
                update(discovery_creators)
                .where(discovery_creators.c.did == profile.did)
                .values(
                    state=target.value,
                    safety_state=(
                        SafetyState.BLOCKED.value
                        if target in {CreatorState.BLOCKED, CreatorState.DELETED}
                        else safety_state.value
                    ),
                    profile_fetched_at=func.now(),
                )
            )
            self._append_event(
                connection,
                campaign_id=campaign_id,
                entity_type=GraphEntityType.CREATOR,
                entity_id=profile.did,
                from_state=previous,
                to_state=target.value,
                reason="profile.hydrated" if target != CreatorState.BLOCKED else "safety.blocked",
                run_id=run_id,
                metadata={"safety_state": safety_state.value},
            )

    def creator_sampling_status(self, did: str) -> CreatorSamplingStatus | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    discovery_creators.c.state,
                    discovery_creators.c.safety_state,
                    discovery_creators.c.score_version,
                    discovery_creators.c.expansion_score,
                    discovery_creators.c.media_sampled_at,
                ).where(discovery_creators.c.did == did)
            ).one_or_none()
        if row is None:
            return None
        return CreatorSamplingStatus(
            state=CreatorState(row.state),
            safety_state=SafetyState(row.safety_state),
            score_version=row.score_version,
            expansion_score=(
                float(row.expansion_score) if row.expansion_score is not None else None
            ),
            media_sampled_at=row.media_sampled_at,
        )

    def expansion_child_dids(self, creator_did: str) -> set[str]:
        """Return creator DIDs already attached by a bounded social expansion."""

        normalized = creator_did.strip()
        if not normalized:
            return set()
        target = discovery_entities.alias("expansion_target_creator")
        statement = (
            select(target.c.canonical_ref)
            .select_from(
                discovery_edges.join(
                    target,
                    target.c.entity_id == discovery_edges.c.target_entity_id,
                )
            )
            .where(
                discovery_edges.c.source_entity_id
                == graph_entity_id(GraphEntityType.CREATOR, normalized),
                target.c.entity_type == GraphEntityType.CREATOR.value,
                discovery_edges.c.edge_type.in_(
                    (
                        GraphEdgeType.FOLLOWS.value,
                        GraphEdgeType.SUGGESTED_FOLLOW.value,
                        GraphEdgeType.GRAPH_SIMILAR.value,
                    )
                ),
            )
        )
        with self.engine.connect() as connection:
            return set(connection.execute(statement).scalars())

    def record_media_results(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        creator_did: str,
        candidates: Sequence[ImageCandidate],
        results: Sequence[FilterResult],
    ) -> None:
        if len(candidates) != len(results):
            raise ValueError("candidates and results must have the same length")
        with self.engine.begin() as connection:
            for candidate, result in zip(candidates, results, strict=True):
                self._record_media_result(connection, creator_did, candidate, result)
            connection.execute(
                update(discovery_creators)
                .where(discovery_creators.c.did == creator_did)
                .values(media_sampled_at=func.now())
            )
            self._append_event(
                connection,
                campaign_id=campaign_id,
                entity_type=GraphEntityType.CREATOR,
                entity_id=creator_did,
                from_state=CreatorState.HYDRATED.value,
                to_state=CreatorState.SAMPLED.value,
                reason="media.sampled",
                run_id=run_id,
                metadata={"media_count": len(candidates)},
            )

    def record_creator_profile(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        profile: CreatorProfile,
        *,
        config_hash: str,
    ) -> None:
        statement = (
            postgresql_insert(discovery_creator_profiles)
            .values(
                did=profile.did,
                score_version=profile.score_version,
                creator_score=profile.creator_score,
                expansion_score=profile.expansion_score,
                sampled_media=profile.sampled_media,
                accepted_media=profile.accepted_media,
                review_media=profile.review_media,
                rejected_media=profile.rejected_media,
                error_media=profile.error_media,
                score_components=profile.score_components,
                profile=profile.profile_summary,
            )
            .on_conflict_do_update(
                index_elements=[discovery_creator_profiles.c.did],
                set_={
                    "score_version": profile.score_version,
                    "creator_score": profile.creator_score,
                    "expansion_score": profile.expansion_score,
                    "sampled_media": profile.sampled_media,
                    "accepted_media": profile.accepted_media,
                    "review_media": profile.review_media,
                    "rejected_media": profile.rejected_media,
                    "error_media": profile.error_media,
                    "score_components": profile.score_components,
                    "profile": profile.profile_summary,
                    "updated_at": func.now(),
                },
            )
        )
        with self.engine.begin() as connection:
            previous = connection.execute(
                select(discovery_creators.c.state).where(discovery_creators.c.did == profile.did)
            ).scalar_one_or_none()
            connection.execute(statement)
            evidence_values = {
                "creator_score": profile.creator_score,
                "expansion_score": profile.expansion_score,
                **profile.score_components,
            }
            for label, score in evidence_values.items():
                evidence_key = stable_key(
                    "discovery-creator-evidence-v1",
                    {
                        "did": profile.did,
                        "label": label,
                        "score_version": profile.score_version,
                        "config_hash": config_hash,
                        "campaign_id": campaign_id,
                        "run_id": run_id,
                    },
                )
                connection.execute(
                    postgresql_insert(discovery_evidence)
                    .values(
                        evidence_id=uuid7(),
                        entity_type=GraphEntityType.CREATOR.value,
                        entity_id=profile.did,
                        namespace="creator_qualification",
                        label=label,
                        score=score,
                        confidence=None,
                        score_kind="versioned_decision_component",
                        model_name="artsearch.discovery.creator_scorer",
                        model_version=profile.score_version,
                        config_hash=config_hash,
                        evidence_key=evidence_key,
                        metadata={
                            **profile.profile_summary,
                            "campaign_id": str(campaign_id),
                            "run_id": run_id,
                        },
                    )
                    .on_conflict_do_nothing(index_elements=[discovery_evidence.c.evidence_key])
                )
            connection.execute(
                update(discovery_creators)
                .where(discovery_creators.c.did == profile.did)
                .values(
                    state=profile.state.value,
                    safety_state=profile.safety_state.value,
                    creator_score=profile.creator_score,
                    expansion_score=profile.expansion_score,
                    score_version=profile.score_version,
                    score_components=profile.score_components,
                    media_sampled_at=func.now(),
                )
            )
            connection.execute(
                update(discovery_entities)
                .where(
                    discovery_entities.c.entity_id
                    == graph_entity_id(GraphEntityType.CREATOR, profile.did)
                )
                .values(
                    discovery_score=profile.creator_score,
                    expansion_score=profile.expansion_score,
                    expansion_state=_expansion_state(profile.state),
                )
            )
            self._append_event(
                connection,
                campaign_id=campaign_id,
                entity_type=GraphEntityType.CREATOR,
                entity_id=profile.did,
                from_state=previous,
                to_state=profile.state.value,
                reason=f"creator.{profile.state.value}",
                run_id=run_id,
                metadata={
                    "creator_score": profile.creator_score,
                    "score_version": profile.score_version,
                },
            )

    def record_coverage_snapshot(
        self,
        campaign_id: uuid.UUID | None,
        *,
        model_bundle_version: str | None = None,
    ) -> uuid.UUID:
        """Persist inexpensive coverage dimensions for trend and frontier analysis."""

        snapshot_id = uuid7()
        with self.engine.begin() as connection:
            states = dict(
                connection.execute(
                    select(discovery_creators.c.state, func.count()).group_by(
                        discovery_creators.c.state
                    )
                ).all()
            )
            safety = dict(
                connection.execute(
                    select(discovery_creators.c.safety_state, func.count()).group_by(
                        discovery_creators.c.safety_state
                    )
                ).all()
            )
            depths = {
                str(depth): int(count)
                for depth, count in connection.execute(
                    select(discovery_creators.c.minimum_depth, func.count()).group_by(
                        discovery_creators.c.minimum_depth
                    )
                ).all()
            }
            popularity_band = _popularity_band_expression()
            popularity = {
                str(band): int(count)
                for band, count in connection.execute(
                    select(
                        popularity_band.label("band"),
                        func.count(),
                    ).group_by(popularity_band)
                ).all()
            }
            sources = {
                str(source_type): int(count)
                for source_type, count in connection.execute(
                    select(discovery_observations.c.source_type, func.count()).group_by(
                        discovery_observations.c.source_type
                    )
                ).all()
            }
            media_states = dict(
                connection.execute(
                    select(discovery_media.c.state, func.count()).group_by(discovery_media.c.state)
                ).all()
            )
            accepted = int(media_states.get(MediaState.ACCEPTED.value, 0))
            connection.execute(
                insert(discovery_coverage_snapshots).values(
                    snapshot_id=snapshot_id,
                    campaign_id=campaign_id,
                    model_bundle_version=model_bundle_version,
                    dimensions={
                        "creator_states": states,
                        "safety_states": safety,
                        "graph_depths": depths,
                        "popularity_bands": popularity,
                        "source_observations": sources,
                        "media_states": media_states,
                    },
                    visual_clusters={},
                    corpus_size=accepted,
                )
            )
        return snapshot_id

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
        payload: dict[str, Any] | None = None,
    ) -> bool:
        values = payload or {}
        did = str(values.get("did", ""))
        task_key = stable_key(
            "discovery-frontier-v1",
            {
                "campaign_id": campaign_id,
                "entity_id": entity_id,
                "action": action.value,
            },
        )
        with self.engine.begin() as connection:
            if did and self._is_excluded(connection, "creator", did, scope="discovery"):
                return False
            active_task = connection.execute(
                select(discovery_frontier_tasks.c.task_id)
                .where(
                    discovery_frontier_tasks.c.entity_id == entity_id,
                    discovery_frontier_tasks.c.action == action.value,
                    discovery_frontier_tasks.c.status.in_(["pending", "retry", "leased"]),
                )
                .limit(1)
            ).scalar_one_or_none()
            if active_task is not None:
                return False
            inserted = connection.execute(
                postgresql_insert(discovery_frontier_tasks)
                .values(
                    task_id=uuid7(),
                    campaign_id=campaign_id,
                    entity_id=entity_id,
                    action=action.value,
                    depth=depth,
                    priority=max(0.0, min(1.0, priority)),
                    budget_class=budget_class,
                    max_attempts=max_attempts,
                    parent_task_id=parent_task_id,
                    idempotency_key=task_key,
                    payload=values,
                )
                .on_conflict_do_nothing(index_elements=[discovery_frontier_tasks.c.idempotency_key])
                .returning(discovery_frontier_tasks.c.task_id)
            ).scalar_one_or_none()
            if inserted is not None:
                connection.execute(
                    update(discovery_entities)
                    .where(discovery_entities.c.entity_id == entity_id)
                    .values(expansion_state="queued")
                )
            return inserted is not None

    def claim_tasks(
        self,
        worker_id: str,
        *,
        limit: int,
        lease_seconds: int,
        campaign_id: uuid.UUID | None = None,
        prefer_high_priority: bool = True,
    ) -> list[FrontierTask]:
        eligible = select(discovery_frontier_tasks.c.task_id).where(
            or_(
                and_(
                    discovery_frontier_tasks.c.status.in_(["pending", "retry"]),
                    discovery_frontier_tasks.c.run_after <= func.now(),
                ),
                and_(
                    discovery_frontier_tasks.c.status == "leased",
                    discovery_frontier_tasks.c.locked_until < func.now(),
                ),
            )
        )
        if campaign_id is not None:
            eligible = eligible.where(discovery_frontier_tasks.c.campaign_id == campaign_id)
        eligible = (
            eligible.order_by(
                (
                    discovery_frontier_tasks.c.priority.desc()
                    if prefer_high_priority
                    else discovery_frontier_tasks.c.priority.asc()
                ),
                discovery_frontier_tasks.c.created_at,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        statement = (
            update(discovery_frontier_tasks)
            .where(discovery_frontier_tasks.c.task_id.in_(eligible))
            .values(
                status=FrontierStatus.LEASED.value,
                attempts=discovery_frontier_tasks.c.attempts + 1,
                locked_at=func.now(),
                locked_until=func.now() + timedelta(seconds=lease_seconds),
                locked_by=worker_id,
            )
            .returning(discovery_frontier_tasks)
        )
        with self.engine.begin() as connection:
            rows = connection.execute(statement).mappings()
            return [_frontier_task(row) for row in rows]

    def complete_task(self, task_id: uuid.UUID, worker_id: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(discovery_frontier_tasks)
                .where(
                    discovery_frontier_tasks.c.task_id == task_id,
                    discovery_frontier_tasks.c.status == FrontierStatus.LEASED.value,
                    discovery_frontier_tasks.c.locked_by == worker_id,
                )
                .values(
                    status=FrontierStatus.COMPLETED.value,
                    completed_at=func.now(),
                    locked_at=None,
                    locked_until=None,
                    locked_by=None,
                    last_error=None,
                )
            )
            return bool(result.rowcount)

    def fail_task(
        self,
        task_id: uuid.UUID,
        worker_id: str,
        error: str,
        *,
        retry_base_seconds: int,
    ) -> FrontierStatus:
        with self.engine.begin() as connection:
            row = connection.execute(
                select(
                    discovery_frontier_tasks.c.attempts,
                    discovery_frontier_tasks.c.max_attempts,
                ).where(
                    discovery_frontier_tasks.c.task_id == task_id,
                    discovery_frontier_tasks.c.status == FrontierStatus.LEASED.value,
                    discovery_frontier_tasks.c.locked_by == worker_id,
                )
            ).one_or_none()
            if row is None:
                raise ValueError("frontier task is not leased by this worker")
            status = (
                FrontierStatus.DEAD
                if int(row.attempts) >= int(row.max_attempts)
                else FrontierStatus.RETRY
            )
            delay = retry_base_seconds * (2 ** max(0, int(row.attempts) - 1))
            connection.execute(
                update(discovery_frontier_tasks)
                .where(discovery_frontier_tasks.c.task_id == task_id)
                .values(
                    status=status.value,
                    run_after=func.now() + timedelta(seconds=delay),
                    locked_at=None,
                    locked_until=None,
                    locked_by=None,
                    last_error=error[:2000],
                )
            )
            return status

    def review_creator(
        self,
        did: str,
        decision: ReviewDecision,
        *,
        reviewer: str,
        note: str = "",
        campaign_id: uuid.UUID | None = None,
    ) -> CreatorState:
        target = {
            ReviewDecision.APPROVE: CreatorState.QUALIFIED,
            ReviewDecision.DEFER: CreatorState.DEFERRED,
            ReviewDecision.REJECT: CreatorState.REJECTED,
            ReviewDecision.BLOCK: CreatorState.BLOCKED,
        }[decision]
        with self.engine.begin() as connection:
            previous = connection.execute(
                select(discovery_creators.c.state).where(discovery_creators.c.did == did)
            ).scalar_one_or_none()
            if previous is None:
                raise KeyError(f"unknown discovery creator: {did}")
            if previous == CreatorState.DELETED.value:
                raise ValueError("deleted creators cannot be reviewed")
            connection.execute(
                update(discovery_creators)
                .where(discovery_creators.c.did == did)
                .values(
                    state=target.value,
                    safety_state=(
                        SafetyState.BLOCKED.value
                        if decision == ReviewDecision.BLOCK
                        else discovery_creators.c.safety_state
                    ),
                    reviewed_at=func.now(),
                    reviewed_by=reviewer,
                    review_note=note[:2000],
                )
            )
            if decision == ReviewDecision.BLOCK:
                self._upsert_exclusion(
                    connection,
                    subject_type="creator",
                    subject_key=did,
                    scope="all",
                    reason_code="operator.creator_block",
                    reason=note,
                    source="human_review",
                    created_by=reviewer,
                )
                connection.execute(
                    update(discovery_frontier_tasks)
                    .where(
                        discovery_frontier_tasks.c.entity_id
                        == graph_entity_id(GraphEntityType.CREATOR, did),
                        discovery_frontier_tasks.c.status.in_(["pending", "retry"]),
                    )
                    .values(status=FrontierStatus.CANCELLED.value)
                )
            self._append_event(
                connection,
                campaign_id=campaign_id,
                entity_type=GraphEntityType.CREATOR,
                entity_id=did,
                from_state=previous,
                to_state=target.value,
                reason=f"human_review.{decision.value}",
                run_id=f"review:{reviewer}",
                metadata={"note": note[:2000]},
            )
        return target

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
        with self.engine.begin() as connection:
            self._upsert_exclusion(
                connection,
                subject_type=subject_type,
                subject_key=subject_key,
                scope=scope,
                reason_code=reason_code,
                reason=reason,
                source=source,
                created_by=created_by,
            )

    def is_excluded(self, subject_type: str, subject_key: str, *, scope: str) -> bool:
        with self.engine.connect() as connection:
            return self._is_excluded(connection, subject_type, subject_key, scope=scope)

    def request_removal(
        self,
        *,
        subject_type: str,
        subject_key: str,
        request_type: str,
        requester_reference: str | None = None,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        request_id = uuid7()
        with self.engine.begin() as connection:
            connection.execute(
                insert(discovery_removal_requests).values(
                    request_id=request_id,
                    subject_type=subject_type,
                    subject_key=subject_key,
                    request_type=request_type,
                    requester_reference=requester_reference,
                    reason=reason,
                    metadata=metadata or {},
                )
            )
        return request_id

    def approve_removal(self, request_id: uuid.UUID, *, reviewer: str) -> None:
        """Block future work and publish a durable removal event for derived stores."""

        with self.engine.begin() as connection:
            request = (
                connection.execute(
                    select(discovery_removal_requests).where(
                        discovery_removal_requests.c.request_id == request_id,
                        discovery_removal_requests.c.status == "open",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if request is None:
                raise KeyError("open removal request not found")
            self._upsert_exclusion(
                connection,
                subject_type=str(request["subject_type"]),
                subject_key=str(request["subject_key"]),
                scope="all",
                reason_code=f"removal.{request['request_type']}",
                reason=str(request["reason"] or ""),
                source="removal_request",
                created_by=reviewer,
            )
            self._mark_deleted(
                connection, str(request["subject_type"]), str(request["subject_key"])
            )
            idempotency_key = stable_key(
                "discovery-removal-v1",
                {"request_id": request_id, "subject": request["subject_key"]},
            )
            connection.execute(
                postgresql_insert(outbox_events)
                .values(
                    event_id=uuid7(),
                    aggregate_type=str(request["subject_type"]),
                    aggregate_id=str(request["subject_key"]),
                    event_type="corpus.remove_subject",
                    payload={
                        "request_id": str(request_id),
                        "subject_type": request["subject_type"],
                        "subject_key": request["subject_key"],
                        "request_type": request["request_type"],
                    },
                    idempotency_key=idempotency_key,
                )
                .on_conflict_do_nothing(index_elements=[outbox_events.c.idempotency_key])
            )
            connection.execute(
                update(discovery_removal_requests)
                .where(discovery_removal_requests.c.request_id == request_id)
                .values(status="approved", resolved_at=func.now(), resolved_by=reviewer)
            )

    def review_queue(
        self,
        *,
        state: str = CreatorState.NEEDS_REVIEW.value,
        offset: int = 0,
    ) -> dict[str, Any]:
        if state not in {item.value for item in CreatorState} | {"all"}:
            raise ValueError("unsupported creator state")
        if offset < 0:
            raise ValueError("invalid review pagination")
        conditions = [] if state == "all" else [discovery_creators.c.state == state]
        statement = (
            select(
                discovery_creators,
                discovery_creator_profiles.c.sampled_media,
                discovery_creator_profiles.c.accepted_media,
                discovery_creator_profiles.c.review_media,
                discovery_creator_profiles.c.rejected_media,
                discovery_creator_profiles.c.error_media,
                discovery_creator_profiles.c.profile,
            )
            .select_from(
                discovery_creators.outerjoin(
                    discovery_creator_profiles,
                    discovery_creator_profiles.c.did == discovery_creators.c.did,
                )
            )
            .where(*conditions)
            .order_by(
                discovery_creators.c.creator_score.desc().nullslast(),
                discovery_creators.c.first_seen_at,
            )
        )
        with self.engine.connect() as connection:
            total = int(
                connection.execute(
                    select(func.count()).select_from(discovery_creators).where(*conditions)
                ).scalar_one()
            )
            if total == 0:
                return {"item": None, "offset": 0, "total": 0}
            resolved_offset = min(offset, total - 1)
            row = dict(
                connection.execute(statement.offset(resolved_offset).limit(1)).mappings().one()
            )
            source_rows = connection.execute(
                select(
                    discovery_observations.c.source_type,
                    discovery_observations.c.source_ref,
                    discovery_observations.c.query,
                    discovery_observations.c.observed_at,
                )
                .where(discovery_observations.c.candidate_did == row["did"])
                .order_by(discovery_observations.c.observed_at.desc())
                .limit(20)
            ).mappings()
            row["observations"] = [dict(item) for item in source_rows]
            media_rows = connection.execute(
                select(
                    discovery_media.c.candidate_id,
                    discovery_media.c.state,
                    discovery_media.c.width,
                    discovery_media.c.height,
                )
                .select_from(
                    discovery_media.join(
                        discovery_posts,
                        discovery_posts.c.uri == discovery_media.c.post_uri,
                    )
                )
                .where(discovery_posts.c.author_did == row["did"])
                .order_by(
                    case(
                        (discovery_media.c.state == MediaState.ACCEPTED.value, 0),
                        (discovery_media.c.state == MediaState.REVIEW.value, 1),
                        else_=2,
                    ),
                    discovery_media.c.first_seen_at.desc(),
                )
                .limit(8)
            ).mappings()
            row["media"] = [dict(item) for item in media_rows]
            return {
                "item": _json_ready(row),
                "offset": resolved_offset,
                "total": total,
            }

    def snapshot(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            creator_states = dict(
                connection.execute(
                    select(discovery_creators.c.state, func.count())
                    .group_by(discovery_creators.c.state)
                    .order_by(discovery_creators.c.state)
                ).all()
            )
            safety_states = dict(
                connection.execute(
                    select(discovery_creators.c.safety_state, func.count())
                    .group_by(discovery_creators.c.safety_state)
                    .order_by(discovery_creators.c.safety_state)
                ).all()
            )
            task_states = dict(
                connection.execute(
                    select(discovery_frontier_tasks.c.status, func.count())
                    .group_by(discovery_frontier_tasks.c.status)
                    .order_by(discovery_frontier_tasks.c.status)
                ).all()
            )
            task_actions = dict(
                connection.execute(
                    select(discovery_frontier_tasks.c.action, func.count())
                    .group_by(discovery_frontier_tasks.c.action)
                    .order_by(discovery_frontier_tasks.c.action)
                ).all()
            )
            source_yield = [
                {"source_type": row.source_type, "observations": int(row.observations)}
                for row in connection.execute(
                    select(
                        discovery_observations.c.source_type,
                        func.count().label("observations"),
                    )
                    .group_by(discovery_observations.c.source_type)
                    .order_by(func.count().desc())
                )
            ]
            counts = connection.execute(
                select(
                    select(func.count()).select_from(discovery_creators).scalar_subquery(),
                    select(func.count()).select_from(discovery_observations).scalar_subquery(),
                    select(func.count()).select_from(discovery_edges).scalar_subquery(),
                    select(func.count())
                    .select_from(discovery_exclusions)
                    .where(discovery_exclusions.c.active.is_(True))
                    .scalar_subquery(),
                    select(func.count())
                    .select_from(discovery_removal_requests)
                    .where(discovery_removal_requests.c.status == "open")
                    .scalar_subquery(),
                )
            ).one()
            campaigns = [
                _json_ready(dict(row))
                for row in connection.execute(
                    select(discovery_campaigns)
                    .order_by(discovery_campaigns.c.started_at.desc())
                    .limit(10)
                ).mappings()
            ]
            coverage = (
                connection.execute(
                    select(discovery_coverage_snapshots)
                    .order_by(discovery_coverage_snapshots.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return {
            "status": "online",
            "creators": int(counts[0]),
            "observations": int(counts[1]),
            "edges": int(counts[2]),
            "active_exclusions": int(counts[3]),
            "open_removal_requests": int(counts[4]),
            "creator_states": creator_states,
            "safety_states": safety_states,
            "frontier_states": task_states,
            "frontier_actions": task_actions,
            "source_yield": source_yield,
            "campaigns": campaigns,
            "latest_coverage": _json_ready(dict(coverage)) if coverage is not None else None,
        }

    def creator_graph_edges(
        self,
        creator_dids: Sequence[str],
        *,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Return bounded creator-to-creator edges for a known corpus subgraph."""

        dids = tuple(dict.fromkeys(value for value in creator_dids if value))
        if not dids:
            return []
        if limit <= 0:
            raise ValueError("creator graph edge limit must be positive")
        source = discovery_entities.alias("source_creator")
        target = discovery_entities.alias("target_creator")
        statement = (
            select(
                source.c.canonical_ref.label("source_did"),
                target.c.canonical_ref.label("target_did"),
                discovery_edges.c.edge_type,
                discovery_edges.c.confidence,
                discovery_edges.c.support_count,
            )
            .select_from(
                discovery_edges.join(
                    source,
                    source.c.entity_id == discovery_edges.c.source_entity_id,
                ).join(
                    target,
                    target.c.entity_id == discovery_edges.c.target_entity_id,
                )
            )
            .where(
                source.c.entity_type == GraphEntityType.CREATOR.value,
                target.c.entity_type == GraphEntityType.CREATOR.value,
                source.c.canonical_ref.in_(dids),
                target.c.canonical_ref.in_(dids),
            )
            .order_by(
                discovery_edges.c.support_count.desc(),
                discovery_edges.c.confidence.desc(),
                discovery_edges.c.last_seen_at.desc(),
            )
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    def creator_graph_nodes(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Return bounded creator nodes needed by the local corpus-map dashboard."""

        if limit <= 0:
            raise ValueError("creator graph node limit must be positive")
        statement = (
            select(
                discovery_creators.c.did,
                discovery_creators.c.handle,
                discovery_creators.c.display_name,
                discovery_creators.c.state,
                discovery_creators.c.safety_state,
                discovery_creators.c.minimum_depth,
                discovery_creators.c.discovery_count,
                discovery_creators.c.creator_score,
                discovery_creators.c.last_seen_at,
            )
            .where(discovery_creators.c.state != CreatorState.DELETED.value)
            .order_by(
                case(
                    (discovery_creators.c.state == CreatorState.QUALIFIED.value, 0),
                    (discovery_creators.c.state == CreatorState.NEEDS_REVIEW.value, 1),
                    else_=2,
                ),
                discovery_creators.c.last_seen_at.desc(),
            )
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    @staticmethod
    def _upsert_graph_entity(
        connection: Any,
        entity_id: str,
        entity_type: GraphEntityType,
        canonical_ref: str,
        depth: int,
        metadata: dict[str, Any],
    ) -> None:
        connection.execute(
            postgresql_insert(discovery_entities)
            .values(
                entity_id=entity_id,
                entity_type=entity_type.value,
                canonical_ref=canonical_ref,
                minimum_depth=depth,
                metadata=metadata,
            )
            .on_conflict_do_update(
                index_elements=[discovery_entities.c.entity_id],
                set_={
                    "last_seen_at": func.now(),
                    "minimum_depth": func.least(discovery_entities.c.minimum_depth, depth),
                    "metadata": discovery_entities.c.metadata.op("||")(metadata),
                },
            )
        )

    @staticmethod
    def _upsert_creator(
        connection: Any,
        profile: CreatorView,
        *,
        depth: int,
        safety_state: SafetyState,
    ) -> None:
        values = {
            "did": profile.did,
            "handle": profile.handle,
            "display_name": profile.display_name,
            "description": profile.description,
            "avatar_url": profile.avatar_url,
            "followers_count": profile.followers_count,
            "follows_count": profile.follows_count,
            "posts_count": profile.posts_count,
            "labels": list(profile.labels),
            "minimum_depth": depth,
            "safety_state": safety_state.value,
            "profile_metadata": profile.metadata,
        }
        connection.execute(
            postgresql_insert(discovery_creators)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[discovery_creators.c.did],
                set_={
                    "handle": profile.handle,
                    "display_name": profile.display_name,
                    "description": profile.description,
                    "avatar_url": profile.avatar_url,
                    "followers_count": profile.followers_count,
                    "follows_count": profile.follows_count,
                    "posts_count": profile.posts_count,
                    "labels": list(profile.labels),
                    "minimum_depth": func.least(discovery_creators.c.minimum_depth, depth),
                    "safety_state": safety_state.value,
                    "profile_metadata": discovery_creators.c.profile_metadata.op("||")(
                        profile.metadata
                    ),
                    "last_seen_at": func.now(),
                },
            )
        )

    def _record_edge(
        self,
        connection: Any,
        *,
        source_entity_id: str,
        target_entity_id: str,
        edge_type: GraphEdgeType,
        confidence: float,
        observation_id: uuid.UUID | None,
        provenance_source: str,
        metadata: dict[str, Any],
        evidence_ref: str | None = None,
    ) -> None:
        edge_id = uuid7()
        resolved_edge_id = connection.execute(
            postgresql_insert(discovery_edges)
            .values(
                edge_id=edge_id,
                source_entity_id=source_entity_id,
                target_entity_id=target_entity_id,
                edge_type=edge_type.value,
                confidence=confidence,
                metadata=metadata,
            )
            .on_conflict_do_update(
                constraint="uq_discovery_graph_edge",
                set_={
                    "last_seen_at": func.now(),
                    "confidence": func.greatest(discovery_edges.c.confidence, confidence),
                },
            )
            .returning(discovery_edges.c.edge_id)
        ).scalar_one()
        evidence_key = stable_key(
            "discovery-edge-evidence-v1",
            {
                "edge_id": resolved_edge_id,
                "observation_id": observation_id,
                "evidence_ref": evidence_ref,
                "provenance_source": provenance_source,
            },
        )
        evidence_inserted = connection.execute(
            postgresql_insert(discovery_edge_evidence)
            .values(
                edge_evidence_id=uuid7(),
                edge_id=resolved_edge_id,
                observation_id=observation_id,
                provenance_source=provenance_source,
                confidence=confidence,
                evidence_key=evidence_key,
                metadata=metadata,
            )
            .on_conflict_do_nothing(index_elements=[discovery_edge_evidence.c.evidence_key])
            .returning(discovery_edge_evidence.c.edge_evidence_id)
        ).scalar_one_or_none()
        if evidence_inserted is not None:
            connection.execute(
                update(discovery_edges)
                .where(discovery_edges.c.edge_id == resolved_edge_id)
                .values(support_count=discovery_edges.c.support_count + 1)
            )

    def _record_media_result(
        self,
        connection: Any,
        creator_did: str,
        candidate: ImageCandidate,
        result: FilterResult,
    ) -> None:
        if not candidate.post_uri or not candidate.post_cid or candidate.image_index is None:
            return
        creator_entity_id = graph_entity_id(GraphEntityType.CREATOR, creator_did)
        post_entity_id = graph_entity_id(GraphEntityType.POST, candidate.post_uri)
        # Candidate IDs are stable for a post/image position; blob CIDs are versions.
        media_id = candidate.candidate_id
        media_entity_id = graph_entity_id(GraphEntityType.MEDIA, media_id)
        self._upsert_graph_entity(
            connection,
            post_entity_id,
            GraphEntityType.POST,
            candidate.post_uri,
            0,
            {"cid": candidate.post_cid, "author_did": creator_did},
        )
        self._upsert_graph_entity(
            connection,
            media_entity_id,
            GraphEntityType.MEDIA,
            media_id,
            0,
            {
                "candidate_id": candidate.candidate_id,
                "post_uri": candidate.post_uri,
                "blob_cid": candidate.blob_cid,
            },
        )
        edge_evidence_ref = f"{candidate.candidate_id}:{candidate.post_cid}"
        self._record_edge(
            connection,
            source_entity_id=creator_entity_id,
            target_entity_id=post_entity_id,
            edge_type=GraphEdgeType.AUTHORED,
            confidence=1.0,
            observation_id=None,
            provenance_source="author_feed",
            metadata={"post_cid": candidate.post_cid},
            evidence_ref=edge_evidence_ref,
        )
        if candidate.quoted_author_did and candidate.quoted_author_did != creator_did:
            quoted_entity_id = graph_entity_id(
                GraphEntityType.CREATOR,
                candidate.quoted_author_did,
            )
            self._upsert_graph_entity(
                connection,
                quoted_entity_id,
                GraphEntityType.CREATOR,
                candidate.quoted_author_did,
                0,
                {"hydrated": False},
            )
            self._record_edge(
                connection,
                source_entity_id=creator_entity_id,
                target_entity_id=quoted_entity_id,
                edge_type=GraphEdgeType.QUOTED,
                confidence=0.8,
                observation_id=None,
                provenance_source="post_embed",
                metadata={"post_uri": candidate.post_uri},
                evidence_ref=edge_evidence_ref,
            )
        self._record_edge(
            connection,
            source_entity_id=post_entity_id,
            target_entity_id=media_entity_id,
            edge_type=GraphEdgeType.ATTACHED_MEDIA,
            confidence=1.0,
            observation_id=None,
            provenance_source="post_embed",
            metadata={"image_index": candidate.image_index},
            evidence_ref=edge_evidence_ref,
        )
        connection.execute(
            postgresql_insert(discovery_posts)
            .values(
                uri=candidate.post_uri,
                cid=candidate.post_cid,
                author_did=creator_did,
                created_at=candidate.created_at,
                text=candidate.post_text[:5000],
                labels=candidate.content_labels,
                is_repost=candidate.is_repost,
                is_quote_post=candidate.is_quote_post,
            )
            .on_conflict_do_update(
                index_elements=[discovery_posts.c.uri],
                set_={
                    "cid": candidate.post_cid,
                    "text": candidate.post_text[:5000],
                    "labels": candidate.content_labels,
                    "is_repost": candidate.is_repost,
                    "is_quote_post": candidate.is_quote_post,
                    "last_hydrated_at": func.now(),
                    "deleted_at": None,
                },
            )
        )
        state = {
            FilterDecision.ACCEPT: MediaState.ACCEPTED,
            FilterDecision.REVIEW: MediaState.REVIEW,
            FilterDecision.REJECT: MediaState.REJECTED,
            FilterDecision.ERROR: MediaState.ERROR,
        }[result.decision]
        connection.execute(
            postgresql_insert(discovery_media)
            .values(
                media_id=media_id,
                post_uri=candidate.post_uri,
                blob_cid=candidate.blob_cid,
                media_index=candidate.image_index,
                candidate_id=candidate.candidate_id,
                thumbnail_url=candidate.thumbnail_url,
                fullsize_url=candidate.fullsize_url,
                width=result.width or candidate.declared_width,
                height=result.height or candidate.declared_height,
                state=state.value,
                content_sha256=result.image_sha256,
            )
            .on_conflict_do_update(
                index_elements=[discovery_media.c.media_id],
                set_={
                    "blob_cid": candidate.blob_cid,
                    "thumbnail_url": candidate.thumbnail_url,
                    "fullsize_url": candidate.fullsize_url,
                    "state": state.value,
                    "content_sha256": result.image_sha256,
                    "width": result.width or candidate.declared_width,
                    "height": result.height or candidate.declared_height,
                    "last_seen_at": func.now(),
                    "deleted_at": None,
                },
            )
        )
        model_revision = (
            result.visual_scores.model_revision if result.visual_scores is not None else None
        )
        for namespace, label, score in (
            ("artwork_gate", result.decision.value, result.final_score),
            ("content_class", result.predicted_class.value, result.confidence),
        ):
            evidence_key = stable_key(
                "discovery-classification-evidence-v1",
                {
                    "entity": media_id,
                    "namespace": namespace,
                    "label": label,
                    "model": result.model_version,
                    "revision": model_revision,
                    "config_hash": result.config_hash,
                    "processed_at": result.processed_at,
                },
            )
            connection.execute(
                postgresql_insert(discovery_evidence)
                .values(
                    evidence_id=uuid7(),
                    entity_type=GraphEntityType.MEDIA.value,
                    entity_id=media_id,
                    namespace=namespace,
                    label=label,
                    score=score,
                    confidence=result.confidence,
                    score_kind="decision_score",
                    model_name=result.model_version,
                    model_version=model_revision or "unresolved",
                    config_hash=result.config_hash,
                    evidence_key=evidence_key,
                    metadata={
                        "candidate_id": candidate.candidate_id,
                        "reason_codes": result.reason_codes,
                        "route": result.route,
                    },
                )
                .on_conflict_do_nothing(index_elements=[discovery_evidence.c.evidence_key])
            )

    @staticmethod
    def _append_event(
        connection: Any,
        *,
        campaign_id: uuid.UUID | None,
        entity_type: GraphEntityType,
        entity_id: str,
        from_state: str | None,
        to_state: str,
        reason: str,
        run_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            insert(discovery_pipeline_events).values(
                event_id=uuid7(),
                campaign_id=campaign_id,
                entity_type=entity_type.value,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                run_id=run_id,
                metadata=metadata or {},
            )
        )

    @staticmethod
    def _is_excluded(
        connection: Any,
        subject_type: str,
        subject_key: str,
        *,
        scope: str,
    ) -> bool:
        return (
            connection.execute(
                select(discovery_exclusions.c.exclusion_id)
                .where(
                    discovery_exclusions.c.subject_type == subject_type,
                    discovery_exclusions.c.subject_key == subject_key,
                    discovery_exclusions.c.scope.in_([scope, "all"]),
                    discovery_exclusions.c.active.is_(True),
                    or_(
                        discovery_exclusions.c.expires_at.is_(None),
                        discovery_exclusions.c.expires_at > func.now(),
                    ),
                )
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )

    @staticmethod
    def _upsert_exclusion(
        connection: Any,
        *,
        subject_type: str,
        subject_key: str,
        scope: str,
        reason_code: str,
        reason: str,
        source: str,
        created_by: str | None,
    ) -> None:
        connection.execute(
            postgresql_insert(discovery_exclusions)
            .values(
                exclusion_id=uuid7(),
                subject_type=subject_type,
                subject_key=subject_key,
                scope=scope,
                reason_code=reason_code,
                reason=reason[:2000],
                source=source,
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint="uq_discovery_exclusion",
                set_={
                    "active": True,
                    "reason_code": reason_code,
                    "reason": reason[:2000],
                    "source": source,
                    "created_by": created_by,
                    "expires_at": None,
                },
            )
        )

    @staticmethod
    def _mark_deleted(connection: Any, subject_type: str, subject_key: str) -> None:
        if subject_type == "creator":
            connection.execute(
                update(discovery_creators)
                .where(discovery_creators.c.did == subject_key)
                .values(state=CreatorState.DELETED.value, safety_state=SafetyState.BLOCKED.value)
            )
        elif subject_type == "post":
            connection.execute(
                update(discovery_posts)
                .where(discovery_posts.c.uri == subject_key)
                .values(deleted_at=func.now())
            )
            connection.execute(
                update(discovery_media)
                .where(discovery_media.c.post_uri == subject_key)
                .values(state=MediaState.DELETED.value, deleted_at=func.now())
            )
        elif subject_type == "media":
            connection.execute(
                update(discovery_media)
                .where(discovery_media.c.media_id == subject_key)
                .values(state=MediaState.DELETED.value, deleted_at=func.now())
            )


def _source_entity_type(suggestion: DiscoverySuggestion) -> GraphEntityType:
    if suggestion.graph_parent_type is not None:
        return suggestion.graph_parent_type
    if suggestion.query:
        return GraphEntityType.QUERY
    return {
        "starter_pack_search": GraphEntityType.STARTER_PACK,
        "starter_pack": GraphEntityType.STARTER_PACK,
        "list": GraphEntityType.LIST,
        "feed": GraphEntityType.FEED,
        "outbound_follow": GraphEntityType.CREATOR,
        "suggested_follow": GraphEntityType.CREATOR,
    }.get(suggestion.source_type.value, GraphEntityType.SOURCE)


def _expansion_state(state: CreatorState) -> str:
    return {
        CreatorState.QUALIFIED: "available",
        CreatorState.NEEDS_REVIEW: "deferred",
        CreatorState.DEFERRED: "deferred",
        CreatorState.REJECTED: "rejected",
        CreatorState.BLOCKED: "blocked",
        CreatorState.DELETED: "deleted",
    }.get(state, "complete")


def _popularity_band_expression() -> Any:
    return case(
        (discovery_creators.c.followers_count.is_(None), "unknown"),
        (discovery_creators.c.followers_count < 1_000, "under_1k"),
        (discovery_creators.c.followers_count < 10_000, "1k_10k"),
        (discovery_creators.c.followers_count < 100_000, "10k_100k"),
        else_="over_100k",
    )


def _frontier_task(row: Any) -> FrontierTask:
    return FrontierTask(
        task_id=row["task_id"],
        campaign_id=row["campaign_id"],
        entity_id=str(row["entity_id"]),
        action=ExpansionAction(row["action"]),
        status=FrontierStatus(row["status"]),
        depth=int(row["depth"]),
        priority=float(row["priority"]),
        budget_class=str(row["budget_class"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        run_after=row["run_after"],
        parent_task_id=row["parent_task_id"],
        payload=dict(row["payload"] or {}),
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (datetime, uuid.UUID)):
        return str(value)
    return value
