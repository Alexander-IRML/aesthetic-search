from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence
import uuid

from sqlalchemy import Engine, and_, case, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import Connection

from artsearch.postgres.ids import uuid7
from artsearch.postgres.schema import (
    followed_artists,
    interaction_events,
    outbox_events,
    queries,
    ranking_runs,
    retrieval_candidates,
    result_impressions,
    saved_artworks,
    sessions,
    users,
)


@dataclass(frozen=True)
class QueryStart:
    session_id: uuid.UUID
    query_modality: str
    user_id: uuid.UUID | None = None
    request_id: uuid.UUID = field(default_factory=uuid7)
    query_id: uuid.UUID = field(default_factory=uuid7)
    parent_query_id: uuid.UUID | None = None
    query_text: str | None = None
    query_image_uri: str | None = None
    positive_artwork_ids: tuple[str, ...] = ()
    negative_artwork_ids: tuple[str, ...] = ()
    filters: dict[str, object] = field(default_factory=dict)
    retrieval_weights: dict[str, float] = field(default_factory=dict)
    model_bundle_version: str | None = None
    index_version: str | None = None


@dataclass(frozen=True)
class RankingStage:
    stage: str
    candidate_count: int
    latency_ms: float
    model_version: str | None = None
    index_version: str | None = None
    candidate_artifact_uri: str | None = None
    score_summary: dict[str, object] = field(default_factory=dict)
    ranking_run_id: uuid.UUID = field(default_factory=uuid7)


@dataclass(frozen=True)
class Impression:
    artwork_id: str
    artist_id: str
    result_rank: int
    fusion_score: float | None = None
    clip_score: float | None = None
    style_score: float | None = None
    scores: dict[str, float] = field(default_factory=dict)
    model_bundle_version: str | None = None
    index_version: str | None = None
    impression_id: uuid.UUID = field(default_factory=uuid7)


@dataclass(frozen=True)
class RankedCandidate:
    artwork_id: str
    artist_id: str
    candidate_rank: int
    fusion_score: float | None = None
    clip_score: float | None = None
    clip_rank: int | None = None
    style_score: float | None = None
    style_rank: int | None = None
    scores: dict[str, float] = field(default_factory=dict)
    model_bundle_version: str | None = None
    index_version: str | None = None


@dataclass(frozen=True)
class Interaction:
    session_id: uuid.UUID
    interaction_type: str
    query_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    artwork_id: str | None = None
    artist_id: str | None = None
    result_rank: int | None = None
    value: bool | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    interaction_id: uuid.UUID = field(default_factory=uuid7)


@dataclass(frozen=True)
class OutboxEvent:
    event_id: uuid.UUID
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, object]
    idempotency_key: str
    attempts: int


class TelemetryRepository:
    """Write product telemetry with explicit model, index, and latency evidence."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def upsert_user(
        self,
        external_subject: str,
        *,
        display_name: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> uuid.UUID:
        user_id = uuid7()
        statement = (
            postgresql_insert(users)
            .values(
                user_id=user_id,
                external_subject=external_subject,
                display_name=display_name,
                metadata=metadata or {},
                last_seen_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=[users.c.external_subject],
                set_={
                    "display_name": display_name,
                    "metadata": metadata or {},
                    "last_seen_at": func.now(),
                },
            )
            .returning(users.c.user_id)
        )
        with self.engine.begin() as connection:
            return connection.execute(statement).scalar_one()

    def start_session(
        self,
        *,
        user_id: uuid.UUID | None = None,
        client_session_key: str | None = None,
        client_metadata: dict[str, object] | None = None,
    ) -> uuid.UUID:
        session_id = uuid7()
        with self.engine.begin() as connection:
            connection.execute(
                insert(sessions).values(
                    session_id=session_id,
                    user_id=user_id,
                    client_session_key=client_session_key,
                    client_metadata=client_metadata or {},
                )
            )
        return session_id

    def resolve_session(
        self,
        client_session_key: str,
        *,
        user_id: uuid.UUID | None = None,
        client_metadata: dict[str, object] | None = None,
    ) -> uuid.UUID:
        """Return one durable session for a browser-generated opaque key."""

        if not client_session_key.strip():
            raise ValueError("client_session_key must not be empty")
        session_id = uuid7()
        statement = (
            postgresql_insert(sessions)
            .values(
                session_id=session_id,
                user_id=user_id,
                client_session_key=client_session_key,
                client_metadata=client_metadata or {},
            )
            .on_conflict_do_update(
                index_elements=[sessions.c.client_session_key],
                set_={
                    "last_seen_at": func.now(),
                    "client_metadata": client_metadata or {},
                },
            )
            .returning(sessions.c.session_id)
        )
        with self.engine.begin() as connection:
            return connection.execute(statement).scalar_one()

    def start_query(self, query: QueryStart) -> uuid.UUID:
        with self.engine.begin() as connection:
            connection.execute(
                insert(queries).values(
                    query_id=query.query_id,
                    request_id=query.request_id,
                    session_id=query.session_id,
                    user_id=query.user_id,
                    parent_query_id=query.parent_query_id,
                    query_modality=query.query_modality,
                    query_text=query.query_text,
                    query_image_uri=query.query_image_uri,
                    positive_artwork_ids=list(query.positive_artwork_ids),
                    negative_artwork_ids=list(query.negative_artwork_ids),
                    filters=query.filters,
                    retrieval_weights=query.retrieval_weights,
                    model_bundle_version=query.model_bundle_version,
                    index_version=query.index_version,
                )
            )
        return query.query_id

    def complete_query(
        self,
        query_id: uuid.UUID,
        *,
        candidate_count: int,
        result_count: int,
        latency_ms: float,
        latency_breakdown: dict[str, float],
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(queries)
                .where(queries.c.query_id == query_id)
                .values(
                    candidate_count=candidate_count,
                    result_count=result_count,
                    latency_ms=latency_ms,
                    latency_breakdown=latency_breakdown,
                    status="completed",
                    completed_at=func.now(),
                )
            )

    def fail_query(
        self,
        query_id: uuid.UUID,
        *,
        error_type: str,
        latency_ms: float,
        latency_breakdown: dict[str, float] | None = None,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(queries)
                .where(queries.c.query_id == query_id)
                .values(
                    status="error",
                    error_type=error_type,
                    latency_ms=latency_ms,
                    latency_breakdown=latency_breakdown or {},
                    completed_at=func.now(),
                )
            )

    def record_ranking_stage(self, query_id: uuid.UUID, stage: RankingStage) -> uuid.UUID:
        with self.engine.begin() as connection:
            connection.execute(
                insert(ranking_runs).values(
                    ranking_run_id=stage.ranking_run_id,
                    query_id=query_id,
                    stage=stage.stage,
                    candidate_count=stage.candidate_count,
                    model_version=stage.model_version,
                    index_version=stage.index_version,
                    latency_ms=stage.latency_ms,
                    candidate_artifact_uri=stage.candidate_artifact_uri,
                    score_summary=stage.score_summary,
                )
            )
        return stage.ranking_run_id

    def record_impressions(
        self,
        query_id: uuid.UUID,
        session_id: uuid.UUID,
        values: Sequence[Impression],
    ) -> None:
        if not values:
            return
        rows = [
            {
                "impression_id": item.impression_id,
                "query_id": query_id,
                "session_id": session_id,
                "artwork_id": item.artwork_id,
                "artist_id": item.artist_id,
                "result_rank": item.result_rank,
                "fusion_score": item.fusion_score,
                "clip_score": item.clip_score,
                "style_score": item.style_score,
                "scores": item.scores,
                "model_bundle_version": item.model_bundle_version,
                "index_version": item.index_version,
            }
            for item in values
        ]
        with self.engine.begin() as connection:
            connection.execute(insert(result_impressions), rows)

    def record_candidates(
        self,
        query_id: uuid.UUID,
        values: Sequence[RankedCandidate],
    ) -> None:
        """Persist the bounded ranking pool separately from displayed impressions."""

        if not values:
            return
        rows = [
            {
                "query_id": query_id,
                "artwork_id": item.artwork_id,
                "artist_id": item.artist_id,
                "candidate_rank": item.candidate_rank,
                "fusion_score": item.fusion_score,
                "clip_score": item.clip_score,
                "clip_rank": item.clip_rank,
                "style_score": item.style_score,
                "style_rank": item.style_rank,
                "scores": item.scores,
                "model_bundle_version": item.model_bundle_version,
                "index_version": item.index_version,
            }
            for item in values
        ]
        with self.engine.begin() as connection:
            connection.execute(insert(retrieval_candidates), rows)

    def record_relevance_feedback(
        self,
        *,
        client_session_key: str,
        query_id: uuid.UUID,
        artwork_id: str,
        grade: str,
    ) -> tuple[uuid.UUID, int] | None:
        """Append a grade only when the query result belongs to the browser session."""

        relevance = {"bad": 0, "good": 1, "great": 2}.get(grade)
        if relevance is None:
            raise ValueError("feedback grade must be great, good, or bad")
        target_statement = (
            select(
                queries.c.session_id,
                queries.c.user_id,
                result_impressions.c.artist_id,
                result_impressions.c.result_rank,
            )
            .select_from(
                queries.join(sessions, sessions.c.session_id == queries.c.session_id).join(
                    result_impressions,
                    and_(
                        result_impressions.c.query_id == queries.c.query_id,
                        result_impressions.c.session_id == queries.c.session_id,
                    ),
                )
            )
            .where(
                queries.c.query_id == query_id,
                queries.c.status == "completed",
                sessions.c.client_session_key == client_session_key,
                result_impressions.c.artwork_id == artwork_id,
            )
        )
        with self.engine.begin() as connection:
            target = connection.execute(target_statement).first()
            if target is None:
                return None
            interaction_id = uuid7()
            connection.execute(
                insert(interaction_events).values(
                    interaction_id=interaction_id,
                    query_id=query_id,
                    session_id=target.session_id,
                    user_id=target.user_id,
                    artwork_id=artwork_id,
                    artist_id=target.artist_id,
                    interaction_type="relevance_feedback",
                    result_rank=target.result_rank,
                    value=relevance > 0,
                    metadata={"grade": grade, "relevance": relevance},
                )
            )
        return interaction_id, int(target.result_rank)

    def record_interaction(self, interaction: Interaction) -> uuid.UUID:
        with self.engine.begin() as connection:
            connection.execute(
                insert(interaction_events).values(
                    interaction_id=interaction.interaction_id,
                    query_id=interaction.query_id,
                    session_id=interaction.session_id,
                    user_id=interaction.user_id,
                    artwork_id=interaction.artwork_id,
                    artist_id=interaction.artist_id,
                    interaction_type=interaction.interaction_type,
                    result_rank=interaction.result_rank,
                    value=interaction.value,
                    metadata=interaction.metadata,
                )
            )
            self._apply_materialized_preference(connection, interaction)
        return interaction.interaction_id

    @staticmethod
    def _apply_materialized_preference(
        connection: Connection,
        interaction: Interaction,
    ) -> None:
        if interaction.user_id is None:
            return
        if interaction.interaction_type == "save" and interaction.artwork_id:
            connection.execute(
                postgresql_insert(saved_artworks)
                .values(user_id=interaction.user_id, artwork_id=interaction.artwork_id)
                .on_conflict_do_nothing()
            )
        elif interaction.interaction_type == "unsave" and interaction.artwork_id:
            connection.execute(
                delete(saved_artworks).where(
                    saved_artworks.c.user_id == interaction.user_id,
                    saved_artworks.c.artwork_id == interaction.artwork_id,
                )
            )
        elif interaction.interaction_type == "follow_artist" and interaction.artist_id:
            connection.execute(
                postgresql_insert(followed_artists)
                .values(user_id=interaction.user_id, artist_id=interaction.artist_id)
                .on_conflict_do_nothing()
            )
        elif interaction.interaction_type == "unfollow_artist" and interaction.artist_id:
            connection.execute(
                delete(followed_artists).where(
                    followed_artists.c.user_id == interaction.user_id,
                    followed_artists.c.artist_id == interaction.artist_id,
                )
            )


class OutboxRepository:
    """Claim and settle durable at-least-once publication events."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def enqueue(
        connection: Connection,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> uuid.UUID:
        event_id = uuid7()
        statement = (
            postgresql_insert(outbox_events)
            .values(
                event_id=event_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_update(
                index_elements=[outbox_events.c.idempotency_key],
                set_={"idempotency_key": outbox_events.c.idempotency_key},
            )
            .returning(outbox_events.c.event_id)
        )
        return connection.execute(statement).scalar_one()

    def claim(
        self,
        worker_id: str,
        *,
        limit: int = 100,
        event_type: str | None = None,
    ) -> list[OutboxEvent]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.engine.begin() as connection:
            conditions = [
                outbox_events.c.status == "pending",
                outbox_events.c.available_at <= func.now(),
            ]
            if event_type is not None:
                conditions.append(outbox_events.c.event_type == event_type)
            claimable = (
                select(outbox_events.c.event_id)
                .where(*conditions)
                .order_by(outbox_events.c.created_at, outbox_events.c.event_id)
                .limit(limit)
                .with_for_update(skip_locked=True)
                .cte("claimable")
            )
            rows = connection.execute(
                update(outbox_events)
                .where(outbox_events.c.event_id.in_(select(claimable.c.event_id)))
                .values(
                    status="processing",
                    attempts=outbox_events.c.attempts + 1,
                    locked_at=func.now(),
                    locked_by=worker_id,
                )
                .returning(
                    outbox_events.c.event_id,
                    outbox_events.c.aggregate_type,
                    outbox_events.c.aggregate_id,
                    outbox_events.c.event_type,
                    outbox_events.c.payload,
                    outbox_events.c.idempotency_key,
                    outbox_events.c.attempts,
                )
            ).mappings()
            return [OutboxEvent(**row) for row in rows]

    def mark_published(self, event_id: uuid.UUID, worker_id: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(outbox_events)
                .where(
                    outbox_events.c.event_id == event_id,
                    outbox_events.c.status == "processing",
                    outbox_events.c.locked_by == worker_id,
                )
                .values(
                    status="published",
                    published_at=func.now(),
                    locked_at=None,
                    locked_by=None,
                    last_error=None,
                )
            )
            return result.rowcount == 1

    def mark_failed(
        self,
        event_id: uuid.UUID,
        worker_id: str,
        error: str,
        *,
        retry_after_seconds: int = 30,
        max_attempts: int = 8,
    ) -> bool:
        if retry_after_seconds < 0 or max_attempts <= 0:
            raise ValueError("retry_after_seconds and max_attempts are invalid")
        with self.engine.begin() as connection:
            result = connection.execute(
                update(outbox_events)
                .where(
                    outbox_events.c.event_id == event_id,
                    outbox_events.c.status == "processing",
                    outbox_events.c.locked_by == worker_id,
                )
                .values(
                    status=case(
                        (outbox_events.c.attempts >= max_attempts, "dead"),
                        else_="pending",
                    ),
                    available_at=func.now() + text("make_interval(secs => :retry_after_seconds)"),
                    locked_at=None,
                    locked_by=None,
                    last_error=error[:4000],
                ),
                {"retry_after_seconds": retry_after_seconds},
            )
            return result.rowcount == 1

    def release_stale(self, *, older_than_seconds: int = 900) -> int:
        if older_than_seconds <= 0:
            raise ValueError("older_than_seconds must be positive")
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE ingest.outbox_events
                       SET status = 'pending', locked_at = NULL, locked_by = NULL
                     WHERE status = 'processing'
                       AND locked_at < now() - make_interval(secs => :seconds)
                    """
                ),
                {"seconds": older_than_seconds},
            )
            return int(result.rowcount or 0)


def enqueue_catalog_change(
    connection: Connection,
    *,
    artwork_id: str,
    content_hash: str,
    collection_name: str,
) -> uuid.UUID:
    return OutboxRepository.enqueue(
        connection,
        aggregate_type="artwork",
        aggregate_id=artwork_id,
        event_type="qdrant.upsert_artwork",
        payload={
            "artwork_id": artwork_id,
            "content_hash": content_hash,
            "collection_name": collection_name,
        },
        idempotency_key=f"qdrant:{collection_name}:{artwork_id}:{content_hash}",
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
