from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

from artsearch.postgres.ids import uuid7
from artsearch.postgres.repositories import (
    Impression,
    QueryStart,
    RankedCandidate,
    RankingStage,
    TelemetryRepository,
)
from artsearch.retrieval.qdrant import QdrantSearchHit


SESSION_NAMESPACE = uuid.UUID("3caad30d-4168-47c4-b1b4-5ae94ddbb2ae")


@dataclass(frozen=True)
class QueryTelemetryContext:
    query_id: uuid.UUID
    session_id: uuid.UUID


@dataclass(frozen=True)
class FeedbackTelemetryReceipt:
    interaction_id: uuid.UUID
    result_rank: int


class SearchTelemetry(Protocol):
    @property
    def feedback_enabled(self) -> bool: ...

    def start(
        self,
        *,
        client_session_key: str,
        model_bundle_version: str,
        index_version: str,
        artist_id: str | None = None,
    ) -> QueryTelemetryContext: ...

    def complete(
        self,
        context: QueryTelemetryContext,
        *,
        hits: Sequence[QdrantSearchHit],
        model_bundle_version: str,
        index_version: str,
        timings: dict[str, float],
        displayed_count: int,
    ) -> None: ...

    def fail(
        self,
        context: QueryTelemetryContext,
        *,
        error_type: str,
        latency_ms: float,
        timings: dict[str, float],
    ) -> None: ...

    def feedback(
        self,
        *,
        client_session_key: str,
        query_id: uuid.UUID,
        artwork_id: str,
        grade: str,
    ) -> FeedbackTelemetryReceipt | None: ...


class NoopSearchTelemetry:
    @property
    def feedback_enabled(self) -> bool:
        return False

    def start(
        self,
        *,
        client_session_key: str,
        model_bundle_version: str,
        index_version: str,
        artist_id: str | None = None,
    ) -> QueryTelemetryContext:
        return QueryTelemetryContext(
            query_id=uuid7(),
            session_id=uuid.uuid5(SESSION_NAMESPACE, client_session_key),
        )

    def complete(
        self,
        context: QueryTelemetryContext,
        *,
        hits: Sequence[QdrantSearchHit],
        model_bundle_version: str,
        index_version: str,
        timings: dict[str, float],
        displayed_count: int,
    ) -> None:
        return None

    def fail(
        self,
        context: QueryTelemetryContext,
        *,
        error_type: str,
        latency_ms: float,
        timings: dict[str, float],
    ) -> None:
        return None

    def feedback(
        self,
        *,
        client_session_key: str,
        query_id: uuid.UUID,
        artwork_id: str,
        grade: str,
    ) -> FeedbackTelemetryReceipt | None:
        return None


class PostgresSearchTelemetry:
    def __init__(self, repository: TelemetryRepository) -> None:
        self.repository = repository

    @property
    def feedback_enabled(self) -> bool:
        return True

    def start(
        self,
        *,
        client_session_key: str,
        model_bundle_version: str,
        index_version: str,
        artist_id: str | None = None,
    ) -> QueryTelemetryContext:
        session_id = self.repository.resolve_session(
            client_session_key,
            client_metadata={"surface": "public_search"},
        )
        query = QueryStart(
            session_id=session_id,
            query_modality="image",
            filters={"artist_id": artist_id} if artist_id else {},
            model_bundle_version=model_bundle_version,
            index_version=index_version,
            retrieval_weights={"clip_subject": 1.0, "csd_style": 1.0, "fusion": "rrf"},
        )
        self.repository.start_query(query)
        return QueryTelemetryContext(query_id=query.query_id, session_id=session_id)

    def complete(
        self,
        context: QueryTelemetryContext,
        *,
        hits: Sequence[QdrantSearchHit],
        model_bundle_version: str,
        index_version: str,
        timings: dict[str, float],
        displayed_count: int,
    ) -> None:
        candidate_count = len(hits)
        self.repository.record_ranking_stage(
            context.query_id,
            RankingStage(
                stage="query_embedding",
                candidate_count=1,
                latency_ms=timings.get("encode_ms", 0.0),
                model_version=model_bundle_version,
                index_version=index_version,
            ),
        )
        self.repository.record_ranking_stage(
            context.query_id,
            RankingStage(
                stage="qdrant_rrf",
                candidate_count=candidate_count,
                latency_ms=timings.get("vector_search_ms", 0.0),
                model_version=model_bundle_version,
                index_version=index_version,
                score_summary={
                    "candidate_depth": candidate_count,
                    "displayed": min(displayed_count, candidate_count),
                    "fusion": "rrf",
                },
            ),
        )
        self.repository.record_candidates(
            context.query_id,
            [
                RankedCandidate(
                    artwork_id=hit.artwork_id,
                    artist_id=hit.artist_id,
                    candidate_rank=hit.rank,
                    fusion_score=hit.fusion_score,
                    clip_score=hit.clip_score,
                    clip_rank=hit.clip_rank,
                    style_score=hit.style_score,
                    style_rank=hit.style_rank,
                    scores={
                        "fusion_rank": float(hit.fusion_rank or hit.rank),
                    },
                    model_bundle_version=model_bundle_version,
                    index_version=index_version,
                )
                for hit in hits
            ],
        )
        displayed_hits = hits[:displayed_count]
        self.repository.record_impressions(
            context.query_id,
            context.session_id,
            [
                Impression(
                    artwork_id=hit.artwork_id,
                    artist_id=hit.artist_id,
                    result_rank=hit.rank,
                    fusion_score=hit.fusion_score,
                    clip_score=hit.clip_score,
                    style_score=hit.style_score,
                    scores={
                        "clip_rank": float(hit.clip_rank or 0),
                        "style_rank": float(hit.style_rank or 0),
                    },
                    model_bundle_version=model_bundle_version,
                    index_version=index_version,
                )
                for hit in displayed_hits
            ],
        )
        self.repository.complete_query(
            context.query_id,
            candidate_count=candidate_count,
            result_count=len(displayed_hits),
            latency_ms=timings.get("total_ms", 0.0),
            latency_breakdown=timings,
        )

    def fail(
        self,
        context: QueryTelemetryContext,
        *,
        error_type: str,
        latency_ms: float,
        timings: dict[str, float],
    ) -> None:
        self.repository.fail_query(
            context.query_id,
            error_type=error_type,
            latency_ms=latency_ms,
            latency_breakdown=timings,
        )

    def feedback(
        self,
        *,
        client_session_key: str,
        query_id: uuid.UUID,
        artwork_id: str,
        grade: str,
    ) -> FeedbackTelemetryReceipt | None:
        recorded = self.repository.record_relevance_feedback(
            client_session_key=client_session_key,
            query_id=query_id,
            artwork_id=artwork_id,
            grade=grade,
        )
        if recorded is None:
            return None
        interaction_id, result_rank = recorded
        return FeedbackTelemetryReceipt(
            interaction_id=interaction_id,
            result_rank=result_rank,
        )
