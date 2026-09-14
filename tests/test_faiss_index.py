from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("faiss")

from artsearch.embed.storage import array_to_blob, l2_normalize
from artsearch.ingest.config import (
    AppConfig,
    DuplicateConfig,
    EmbeddingConfig,
    ImageConfig,
    ModelConfig,
    RetrievalConfig,
)
from artsearch.ingest.db import connect, init_db
from artsearch.production.capacity import build_capacity_plan
from artsearch.production.config import FaissConfig, ScaleBudgetConfig
from artsearch.retrieval.faiss_index import (
    FaissIndexError,
    FaissSearchService,
    build_faiss_snapshot,
    evaluate_faiss_snapshot,
    promote_faiss_snapshot,
)


def test_flat_snapshot_build_search_evaluate_and_promote(tmp_path: Path) -> None:
    app = _app_config(tmp_path)
    config = _faiss_config(tmp_path)
    with connect(app.database_path) as connection:
        init_db(connection)
        _add_artwork(
            connection,
            app,
            "query",
            "artist-query",
            [1.0, 0.02, 0.01, 0.005],
            [1.0, 0.02, 0.01, 0.005, 0.002, 0.001],
        )
        _add_artwork(
            connection,
            app,
            "same",
            "artist-a",
            [0.98, 0.12, 0.03, 0.01],
            [0.97, 0.15, 0.04, 0.02, 0.01, 0.005],
        )
        _add_artwork(
            connection,
            app,
            "subject",
            "artist-b",
            [0.82, 0.42, 0.18, 0.07],
            [0.08, 0.94, 0.18, 0.09, 0.03, 0.01],
        )
        _add_artwork(
            connection,
            app,
            "style",
            "artist-c",
            [0.12, 0.93, 0.22, 0.08],
            [0.91, 0.28, 0.11, 0.04, 0.02, 0.01],
        )
        _add_artwork(
            connection,
            app,
            "other",
            "artist-a",
            [0.19, 0.31, 0.91, 0.13],
            [0.14, 0.32, 0.88, 0.19, 0.08, 0.03],
        )

    built = build_faiss_snapshot(
        app.database_path,
        app,
        config,
        snapshot_version="test-v1",
        production_config_hash="config-hash",
    )
    assert built.point_count == 5
    assert built.index_kind == "flat"

    service = FaissSearchService.from_snapshot(config, "test-v1")
    try:
        hits = service.search_by_vectors(
            np.array([1, 0, 0, 0], dtype=np.float32),
            np.array([1, 0, 0, 0, 0, 0], dtype=np.float32),
            top_k=3,
        )
        assert hits[0].artwork_id in {"query", "same"}
        assert all(hit.clip_score is not None for hit in hits)
        assert all(hit.style_score is not None for hit in hits)
        assert len([hit for hit in hits if hit.artist_id == "artist-a"]) <= 1

        artist_hits = service.search_by_vectors(
            np.array([1, 0, 0, 0], dtype=np.float32),
            np.array([1, 0, 0, 0, 0, 0], dtype=np.float32),
            top_k=5,
            artist_id="artist-a",
        )
        assert {hit.artwork_id for hit in artist_hits} == {"same", "other"}

        evaluation = evaluate_faiss_snapshot(service, sample_size=4, top_k=2, seed=7)
        assert evaluation.quality_gate_passed is True, evaluation.to_dict()
        assert all(metric.mean_recall == pytest.approx(1.0) for metric in evaluation.metrics)
    finally:
        service.close()

    evaluation_path = tmp_path / "evaluation.json"
    evaluation_path.write_text(json.dumps(evaluation.to_dict()), encoding="utf-8")
    budget_path = tmp_path / "budget.json"
    budget = build_capacity_plan(config, ScaleBudgetConfig(), target_points=1_000)
    budget_path.write_text(json.dumps(budget.to_dict()), encoding="utf-8")

    stale_budget = budget.to_dict()
    stale_budget["target_points"] = 1
    budget_path.write_text(json.dumps(stale_budget), encoding="utf-8")
    with pytest.raises(FaissIndexError, match="does not cover"):
        promote_faiss_snapshot(
            config,
            "test-v1",
            evaluation_path=evaluation_path,
            budget_path=budget_path,
        )

    invalid_evaluation = evaluation.to_dict()
    invalid_evaluation["metrics"][0]["mean_recall"] = float("nan")
    evaluation_path.write_text(json.dumps(invalid_evaluation), encoding="utf-8")
    budget_path.write_text(json.dumps(budget.to_dict()), encoding="utf-8")
    with pytest.raises(FaissIndexError, match="quality gate"):
        promote_faiss_snapshot(
            config,
            "test-v1",
            evaluation_path=evaluation_path,
            budget_path=budget_path,
        )

    evaluation_path.write_text(json.dumps(evaluation.to_dict()), encoding="utf-8")
    budget_path.write_text(json.dumps(budget.to_dict()), encoding="utf-8")
    promoted = promote_faiss_snapshot(
        config,
        "test-v1",
        evaluation_path=evaluation_path,
        budget_path=budget_path,
    )
    assert promoted["snapshot_version"] == "test-v1"

    active = FaissSearchService.from_active(config)
    try:
        assert active.retrieval_contract.index_version == "test-v1"
    finally:
        active.close()


def _app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        root_dir=tmp_path,
        raw_dir=tmp_path / "data/raw",
        processed_dir=tmp_path / "data/processed",
        database_path=tmp_path / "data/artsearch.db",
        images=ImageConfig(
            canonical_size=448,
            crop_threshold=2.5,
            output_format="jpeg",
            jpeg_quality=95,
            padding_fill_strategy="neutral_gray",
            neutral_gray_value=128,
        ),
        duplicates=DuplicateConfig(phash_distance_threshold=6),
        models=ModelConfig(
            clip_model_name="test/clip",
            clip_model_version="clip-v1",
            csd_model_name="test/csd",
            csd_model_version="csd-v1",
            csd_model_filename="model.pt",
            csd_model_sha256="a" * 64,
            csd_processor_name="test/processor",
            csd_processor_version="processor-v1",
        ),
        embeddings=EmbeddingConfig(batch_size=4, device="cpu"),
        retrieval=RetrievalConfig(
            default_top_k=10,
            demo_output_path=tmp_path / "data/demo.html",
            gallery_output_path=tmp_path / "data/gallery.html",
            shortlist_size=10,
            review_session_count=1,
        ),
    )


def _faiss_config(tmp_path: Path) -> FaissConfig:
    return FaissConfig(
        enabled=True,
        artifact_root=tmp_path / "indexes",
        clip_dimension=4,
        csd_dimension=6,
        clip_pq_subquantizers=2,
        csd_pq_subquantizers=3,
        nlist=2,
        nprobe=1,
        flat_fallback_threshold=100,
        training_sample_size=100,
        add_batch_size=2,
        prefetch_limit=5,
        rerank_limit=5,
        fusion_limit=5,
        max_results_per_artist=1,
        minimum_mean_recall=1.0,
        maximum_search_p95_ms=10_000,
    )


def _add_artwork(
    connection,
    app: AppConfig,
    artwork_id: str,
    artist_id: str,
    clip: list[float],
    csd: list[float],
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO artists (artist_id, display_name, folder_name, source_platform)
        VALUES (?, ?, ?, 'manual')
        """,
        (artist_id, artist_id, artist_id),
    )
    connection.execute(
        """
        INSERT INTO artworks (
            artwork_id, artist_id, raw_path, processed_path, source_platform,
            is_sfw, demo_eligible, validated
        ) VALUES (?, ?, ?, ?, 'manual', 1, 1, 1)
        """,
        (artwork_id, artist_id, f"raw/{artwork_id}.jpg", f"processed/{artwork_id}.jpg"),
    )
    clip_vector = l2_normalize(np.asarray(clip, dtype=np.float32))
    csd_vector = l2_normalize(np.asarray(csd, dtype=np.float32))
    connection.execute(
        """
        INSERT INTO embeddings (
            artwork_id, clip_vector, clip_dim, csd_style, csd_style_dim,
            model_name_clip, model_version_clip, model_name_csd, model_version_csd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artwork_id,
            array_to_blob(clip_vector),
            len(clip),
            array_to_blob(csd_vector),
            len(csd),
            app.models.clip_model_name,
            app.models.clip_model_version,
            app.models.csd_model_name,
            app.models.csd_model_version,
        ),
    )
    connection.commit()
