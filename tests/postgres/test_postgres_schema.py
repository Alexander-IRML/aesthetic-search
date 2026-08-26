from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from artsearch.postgres.schema import (
    embeddings,
    interaction_events,
    metadata,
    outbox_events,
    queries,
    retrieval_candidates,
    result_impressions,
)


def test_postgres_metadata_covers_catalog_ml_workflow_and_product_telemetry() -> None:
    expected = {
        "catalog.artists",
        "catalog.artworks",
        "catalog.artwork_objects",
        "ml.embeddings",
        "ml.vector_index_points",
        "ingest.artwork_filter_decisions",
        "ingest.outbox_events",
        "product.sessions",
        "product.queries",
        "product.retrieval_candidates",
        "product.result_impressions",
        "product.interaction_events",
    }

    assert expected <= set(metadata.tables)
    assert {
        "query_modality",
        "model_bundle_version",
        "index_version",
        "latency_breakdown",
        "candidate_count",
    } <= set(queries.c.keys())
    assert {"result_rank", "fusion_score", "clip_score", "style_score"} <= set(
        result_impressions.c.keys()
    )
    assert {
        "candidate_rank",
        "fusion_score",
        "clip_rank",
        "style_rank",
        "model_bundle_version",
        "index_version",
    } <= set(retrieval_candidates.c.keys())
    interaction_checks = " ".join(
        str(constraint.sqltext)
        for constraint in interaction_events.constraints
        if hasattr(constraint, "sqltext")
    )
    for interaction_type in (
        "click",
        "like",
        "dislike",
        "save",
        "follow_artist",
        "reformulation",
        "relevance_feedback",
    ):
        assert f"'{interaction_type}'" in interaction_checks


def test_vectors_are_compact_archival_halfvec_without_postgres_ann_indexes() -> None:
    ddl = str(CreateTable(embeddings).compile(dialect=postgresql.dialect()))
    index_methods = {
        index.dialect_options["postgresql"].get("using")
        for table in metadata.tables.values()
        for index in table.indexes
    }

    assert "HALFVEC(512)" in ddl
    assert "HALFVEC(768)" in ddl
    assert "hnsw" not in index_methods
    assert "ivfflat" not in index_methods


def test_outbox_has_idempotency_claim_and_retry_state() -> None:
    assert {
        "idempotency_key",
        "status",
        "attempts",
        "available_at",
        "locked_at",
        "locked_by",
        "published_at",
    } <= set(outbox_events.c.keys())
