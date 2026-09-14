from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

from artsearch.postgres.ids import uuid7
from artsearch.postgres.repositories import (
    AsyncTelemetryRepository,
    Impression,
    QueryStart,
    RankedCandidate,
    RankingStage,
)
from artsearch.retrieval.backend import SearchHit


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

    async def start(
        self,
        *,
        client_session_key: str,
        model_bundle_version: str,
        index_version: str,
        vector_backend: str = "qdrant",
        artist_id: str | None = None,
        retrieval_weights: dict[str, float] | None = None,
        visitor_hash: str | None = None,
    ) -> QueryTelemetryContext: ...

    async def record_visit(
        self,
        *,
        client_session_key: str,
        visitor_hash: str,
    ) -> bool: ...

    async def complete(
        self,
        context: QueryTelemetryContext,
        *,
        hits: Sequence[SearchHit],
        model_bundle_version: str,
        index_version: str,
        vector_backend: str = "qdrant",
        timings: dict[str, float],
        displayed_count: int,
    ) -> None: ...

    async def fail(
        self,
        context: QueryTelemetryContext,
        *,
        error_type: str,
        latency_ms: float,
        timings: dict[str, float],
    ) -> None: ...

    async def feedback(
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

    async def start(
        self,
        *,
        client_session_key: str,
        model_bundle_version: str,
        index_version: str,
        vector_backend: str = "qdrant",
        artist_id: str | None = None,
        retrieval_weights: dict[str, float] | None = None,
        visitor_hash: str | None = None,
    ) -> QueryTelemetryContext:
        return QueryTelemetryContext(
            query_id=uuid7(),
            session_id=uuid.uuid5(SESSION_NAMESPACE, client_session_key),
        )

    async def record_visit(
        self,
        *,
        client_session_key: str,
        visitor_hash: str,
    ) -> bool:
        return False

    async def complete(
        self,
        context: QueryTelemetryContext,
        *,
        hits: Sequence[SearchHit],
        model_bundle_version: str,
        index_version: str,
        vector_backend: str = "qdrant",
        timings: dict[str, float],
        displayed_count: int,
    ) -> None:
        return None

    async def fail(
        self,
        context: QueryTelemetryContext,
        *,
        error_type: str,
        latency_ms: float,
        timings: dict[str, float],
    ) -> None:
        return None

    async def feedback(
        self,
        *,
        client_session_key: str,
        query_id: uuid.UUID,
        artwork_id: str,
        grade: str,
    ) -> FeedbackTelemetryReceipt | None:
        return None


class PostgresSearchTelemetry:
    def __init__(self, repository: AsyncTelemetryRepository) -> None:
        self.repository = repository

    @property
    def feedback_enabled(self) -> bool:
        return True

    async def start(
        self,
        *,
        client_session_key: str,
        model_bundle_version: str,
        index_version: str,
        vector_backend: str = "qdrant",
        artist_id: str | None = None,
        retrieval_weights: dict[str, float] | None = None,
        visitor_hash: str | None = None,
    ) -> QueryTelemetryContext:
        client_metadata: dict[str, object] = {"surface": "public_search"}
        if visitor_hash is not None:
            client_metadata.update(
                {
                    "visitor_ip_hash": visitor_hash,
                    "visitor_hash_version": "hmac-sha256-v1",
                }
            )
        session_id = await self.repository.resolve_session(
            client_session_key,
            client_metadata=client_metadata,
        )
        weights: dict[str, object] = {
            "clip_first_half": 1.0,
            "clip_second_half": 1.0,
            "csd_first_half": 1.0,
            "csd_second_half": 1.0,
        }
        weights.update(retrieval_weights or {})
        weights.update({"fusion": "rrf", "vector_backend": vector_backend})
        query = QueryStart(
            session_id=session_id,
            query_modality="image",
            filters={"artist_id": artist_id} if artist_id else {},
            model_bundle_version=model_bundle_version,
            index_version=index_version,
            retrieval_weights=weights,
        )
        await self.repository.start_query(query)
        return QueryTelemetryContext(query_id=query.query_id, session_id=session_id)

    async def record_visit(
        self,
        *,
        client_session_key: str,
        visitor_hash: str,
    ) -> bool:
        await self.repository.resolve_session(
            client_session_key,
            client_metadata={
                "surface": "public_search",
                "visitor_ip_hash": visitor_hash,
                "visitor_hash_version": "hmac-sha256-v1",
            },
        )
        return True

    async def complete(
        self,
        context: QueryTelemetryContext,
        *,
        hits: Sequence[SearchHit],
        model_bundle_version: str,
        index_version: str,
        vector_backend: str = "qdrant",
        timings: dict[str, float],
        displayed_count: int,
    ) -> None:
        candidate_count = len(hits)
        await self.repository.record_ranking_stage(
            context.query_id,
            RankingStage(
                stage="query_embedding",
                candidate_count=1,
                latency_ms=timings.get("encode_ms", 0.0),
                model_version=model_bundle_version,
                index_version=index_version,
            ),
        )
        await self.repository.record_ranking_stage(
            context.query_id,
            RankingStage(
                stage=f"{vector_backend}_rrf",
                candidate_count=candidate_count,
                latency_ms=timings.get("vector_search_ms", 0.0),
                model_version=model_bundle_version,
                index_version=index_version,
                score_summary={
                    "candidate_depth": candidate_count,
                    "displayed": min(displayed_count, candidate_count),
                    "fusion": "rrf",
                    "vector_backend": vector_backend,
                },
            ),
        )
        await self.repository.record_candidates(
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
        await self.repository.record_impressions(
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
        await self.repository.complete_query(
            context.query_id,
            candidate_count=candidate_count,
            result_count=len(displayed_hits),
            latency_ms=timings.get("total_ms", 0.0),
            latency_breakdown=timings,
        )

    async def fail(
        self,
        context: QueryTelemetryContext,
        *,
        error_type: str,
        latency_ms: float,
        timings: dict[str, float],
    ) -> None:
        await self.repository.fail_query(
            context.query_id,
            error_type=error_type,
            latency_ms=latency_ms,
            latency_breakdown=timings,
        )

    async def feedback(
        self,
        *,
        client_session_key: str,
        query_id: uuid.UUID,
        artwork_id: str,
        grade: str,
    ) -> FeedbackTelemetryReceipt | None:
        recorded = await self.repository.record_relevance_feedback(
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
