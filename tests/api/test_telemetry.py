from __future__ import annotations

import uuid

from artsearch.api.telemetry import PostgresSearchTelemetry, QueryTelemetryContext
from artsearch.retrieval.qdrant import QdrantSearchHit


class _Repository:
    def __init__(self) -> None:
        self.candidates = []
        self.impressions = []
        self.completed: dict[str, object] = {}

    def record_ranking_stage(self, query_id, stage) -> None:
        return None

    def record_candidates(self, query_id, values) -> None:
        self.candidates = list(values)

    def record_impressions(self, query_id, session_id, values) -> None:
        self.impressions = list(values)

    def complete_query(self, query_id, **values) -> None:
        self.completed = values

    def record_relevance_feedback(self, **values):
        assert values["grade"] == "great"
        return uuid.UUID("01933333-3333-7333-8333-333333333333"), 4


def test_postgres_telemetry_separates_ranked_candidates_from_impressions() -> None:
    repository = _Repository()
    telemetry = PostgresSearchTelemetry(repository)  # type: ignore[arg-type]
    context = QueryTelemetryContext(
        query_id=uuid.UUID("01922222-2222-7222-8222-222222222222"),
        session_id=uuid.UUID("01911111-1111-7111-8111-111111111111"),
    )

    telemetry.complete(
        context,
        hits=[_hit(rank) for rank in range(1, 21)],
        model_bundle_version="bundle-v1",
        index_version="index-v1",
        timings={"encode_ms": 2.0, "vector_search_ms": 3.0, "total_ms": 9.0},
        displayed_count=10,
    )

    assert len(repository.candidates) == 20
    assert len(repository.impressions) == 10
    assert repository.candidates[-1].candidate_rank == 20
    assert repository.impressions[-1].result_rank == 10
    assert repository.completed["candidate_count"] == 20
    assert repository.completed["result_count"] == 10


def test_postgres_telemetry_returns_feedback_rank_receipt() -> None:
    telemetry = PostgresSearchTelemetry(_Repository())  # type: ignore[arg-type]

    receipt = telemetry.feedback(
        client_session_key="browser-session-1234",
        query_id=uuid.UUID("01922222-2222-7222-8222-222222222222"),
        artwork_id="art-4",
        grade="great",
    )

    assert receipt is not None
    assert receipt.result_rank == 4


def _hit(rank: int) -> QdrantSearchHit:
    return QdrantSearchHit(
        artwork_id=f"art-{rank}",
        artist_id=f"artist-{rank}",
        content_class="finished_illustration",
        score=1.0 / rank,
        rank=rank,
        fusion_score=1.0 / rank,
        fusion_rank=rank,
        clip_score=0.8,
        clip_rank=rank,
        style_score=0.7,
        style_rank=rank,
    )
