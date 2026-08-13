from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3

import numpy as np
import pytest
from qdrant_client import QdrantClient, models as qmodels

from artsearch.embed.storage import ImageEmbeddings, upsert_embedding
from artsearch.ingest.config import (
    AppConfig,
    DuplicateConfig,
    EmbeddingConfig,
    ImageConfig,
    ModelConfig,
    RetrievalConfig,
)
from artsearch.ingest.db import connect, init_db
from artsearch.production.config import QdrantConfig
from artsearch.retrieval.qdrant import (
    QdrantIntegrationError,
    QdrantSearchService,
    build_qdrant_client,
    ensure_qdrant_collection,
    evaluate_qdrant_ann,
    point_id_for_artwork,
    promote_qdrant_alias,
    sync_qdrant_from_sqlite,
)


pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")


def test_collection_schema_alias_and_environment_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app_config(tmp_path)
    config = _qdrant_config()
    client = QdrantClient(":memory:")

    result = ensure_qdrant_collection(client, config, app.models)
    assert result.created is True
    assert result.payload_indexes_created == 9
    assert promote_qdrant_alias(client, config) is True
    assert promote_qdrant_alias(client, config) is False
    assert client.get_aliases().aliases[0].collection_name == config.collection_name

    collection = client.get_collection(config.collection_name)
    vectors = collection.config.params.vectors
    assert isinstance(vectors, dict)
    assert vectors[config.clip_vector_name].size == 2
    assert vectors[config.dino_vector_name].size == 2

    secret_config = config.model_copy(update={"require_api_key": True})
    monkeypatch.delenv(secret_config.api_key_env, raising=False)
    with pytest.raises(QdrantIntegrationError, match=secret_config.api_key_env):
        build_qdrant_client(secret_config)
    monkeypatch.setenv(secret_config.api_key_env, "test-only-secret")
    assert build_qdrant_client(secret_config) is not None


def test_collection_schema_mismatch_is_not_silently_reused(tmp_path: Path) -> None:
    app = _app_config(tmp_path)
    config = _qdrant_config()
    client = QdrantClient(":memory:")
    client.create_collection(
        config.collection_name,
        vectors_config={
            config.clip_vector_name: qmodels.VectorParams(
                size=3,
                distance=qmodels.Distance.COSINE,
                datatype=qmodels.Datatype.FLOAT32,
            ),
            config.dino_vector_name: qmodels.VectorParams(
                size=2,
                distance=qmodels.Distance.COSINE,
                datatype=qmodels.Datatype.FLOAT32,
            ),
        },
    )

    with pytest.raises(QdrantIntegrationError, match="schema mismatch"):
        ensure_qdrant_collection(client, config, app.models)


def test_collection_model_lineage_mismatch_requires_a_new_physical_name(
    tmp_path: Path,
) -> None:
    app = _app_config(tmp_path)
    config = _qdrant_config()
    client = QdrantClient(":memory:")
    ensure_qdrant_collection(client, config, app.models)
    changed_models = replace(app.models, clip_model_version="clip-v2")

    with pytest.raises(QdrantIntegrationError, match="model bundle is incompatible"):
        ensure_qdrant_collection(client, config, changed_models)


def test_sync_is_safe_idempotent_and_prunes_ineligible_points(tmp_path: Path) -> None:
    app = _app_config(tmp_path)
    config = _qdrant_config()
    client = QdrantClient(":memory:")
    with connect(app.database_path) as connection:
        init_db(connection)
        _add_artwork(connection, app, "manual-safe", "artist-a", [1, 0], [1, 0])
        _add_artwork(
            connection,
            app,
            "manual-unknown",
            "artist-b",
            [0, 1],
            [0, 1],
            is_sfw=None,
        )
        _add_artwork(
            connection,
            app,
            "bluesky-accept",
            "artist-c",
            [0.8, 0.2],
            [0.2, 0.8],
            source_platform="bluesky",
            decision="accept",
        )
        _add_artwork(
            connection,
            app,
            "bluesky-review",
            "artist-d",
            [0.7, 0.3],
            [0.3, 0.7],
            source_platform="bluesky",
            decision="review",
        )

        first = sync_qdrant_from_sqlite(connection, app, config, client)
        second = sync_qdrant_from_sqlite(connection, app, config, client)

        assert first.eligible == first.upserted == first.remote_count == 2
        assert first.alias_promoted is True
        assert second.upserted == 0
        assert second.unchanged == 2
        assert second.alias_promoted is False

        stored = client.retrieve(
            config.collection_name,
            ids=[point_id_for_artwork("manual-safe")],
            with_payload=True,
        )[0]
        assert stored.payload["created_at"].endswith("Z")

        connection.execute("UPDATE artworks SET is_sfw = NULL WHERE artwork_id = 'manual-safe'")
        connection.commit()
        pruned = sync_qdrant_from_sqlite(connection, app, config, client)
        assert pruned.eligible == 1
        assert pruned.deleted == 1
        assert pruned.remote_count == 1

        _replace_embedding(connection, app, "bluesky-accept", [0, 1], [1, 0])
        changed = sync_qdrant_from_sqlite(connection, app, config, client)
        assert changed.upserted == 1
        assert changed.unchanged == 0


def test_sync_repairs_remote_count_drift_and_deletes_untracked_points(tmp_path: Path) -> None:
    app = _app_config(tmp_path)
    config = _qdrant_config()
    client = QdrantClient(":memory:")
    with connect(app.database_path) as connection:
        init_db(connection)
        _add_artwork(connection, app, "one", "artist-a", [1, 0], [1, 0])
        sync_qdrant_from_sqlite(connection, app, config, client)
        client.upsert(
            config.collection_name,
            [
                qmodels.PointStruct(
                    id=point_id_for_artwork("untracked"),
                    vector={
                        config.clip_vector_name: [0.0, 1.0],
                        config.dino_vector_name: [0.0, 1.0],
                    },
                    payload={
                        "artwork_id": "untracked",
                        "artist_id": "unknown",
                        "demo_eligible": True,
                        "safety_status": "safe",
                    },
                )
            ],
        )

        repaired = sync_qdrant_from_sqlite(connection, app, config, client)
        assert repaired.forced is True
        assert repaired.upserted == 1
        assert repaired.remote_untracked_deleted == 1
        assert repaired.remote_count == 1


def test_search_fuses_clip_and_dino_then_reranks_patches_and_limits_artists(
    tmp_path: Path,
) -> None:
    app = _app_config(tmp_path)
    config = _qdrant_config().model_copy(
        update={"max_results_per_artist": 1, "patch_rerank_limit": 10}
    )
    client = QdrantClient(":memory:")
    query_patches = _patches([1, 0])
    with connect(app.database_path) as connection:
        init_db(connection)
        _add_artwork(
            connection,
            app,
            "query",
            "query-artist",
            [1, 0],
            [1, 0],
            patches=query_patches,
        )
        _add_artwork(
            connection,
            app,
            "global-winner",
            "artist-a",
            [1, 0],
            [1, 0],
            patches=_patches([0, 1]),
        )
        _add_artwork(
            connection,
            app,
            "patch-winner",
            "artist-b",
            [0.8, 0.2],
            [0.8, 0.2],
            patches=query_patches,
        )
        _add_artwork(
            connection,
            app,
            "same-artist-extra",
            "artist-b",
            [0.7, 0.3],
            [0.7, 0.3],
            patches=query_patches,
        )
        sync_qdrant_from_sqlite(connection, app, config, client)

        service = QdrantSearchService(client, config)
        global_hits = service.search_by_artwork(
            connection,
            app,
            "query",
            top_k=3,
            use_patch_rerank=False,
        )
        reranked = service.search_by_artwork(connection, app, "query", top_k=3)

        assert global_hits[0].artwork_id == "global-winner"
        assert global_hits[0].clip_rank == 1
        assert global_hits[0].dino_rank == 1
        assert reranked[0].artwork_id == "patch-winner"
        assert reranked[0].patch_rank == 1
        assert len([hit for hit in reranked if hit.artist_id == "artist-b"]) == 1
        assert all(hit.fusion_score is not None for hit in reranked)

        clip_hits = service.search_by_clip_vector(np.array([1.0, 0.0]), top_k=3)
        assert clip_hits[0].artwork_id in {"query", "global-winner"}
        assert all(hit.clip_score is not None for hit in clip_hits)

        evaluation = evaluate_qdrant_ann(
            connection,
            app,
            config,
            client,
            sample_size=3,
            top_k=2,
            seed=17,
        )
        assert evaluation.sampled_queries == 3
        assert {metric.vector_name for metric in evaluation.metrics} == {
            config.clip_vector_name,
            config.dino_vector_name,
        }
        assert all(metric.mean_recall == 1.0 for metric in evaluation.metrics)


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
            dino_model_name="test/dino",
            dino_model_version="dino-v1",
        ),
        embeddings=EmbeddingConfig(batch_size=4, device="cpu"),
        retrieval=RetrievalConfig(
            default_top_k=10,
            demo_output_path=tmp_path / "data/demo.html",
            gallery_output_path=tmp_path / "data/gallery.html",
            shortlist_size=10,
            patch_match_top_n=1,
            review_session_count=1,
        ),
    )


def _qdrant_config() -> QdrantConfig:
    return QdrantConfig(
        enabled=True,
        url=":memory:",
        collection_name="artworks_test_v1",
        alias_name="artworks_test_active",
        clip_dimension=2,
        dino_dimension=2,
        datatype="float32",
        batch_size=2,
        prefetch_limit=10,
        fusion_limit=10,
        patch_rerank_limit=10,
        max_results_per_artist=2,
        require_sfw=True,
        require_bluesky_accept=True,
    )


def _add_artwork(
    connection: sqlite3.Connection,
    app: AppConfig,
    artwork_id: str,
    artist_id: str,
    clip: list[float],
    dino: list[float],
    *,
    patches: np.ndarray | None = None,
    is_sfw: bool | None = True,
    source_platform: str = "manual",
    decision: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO artists (
            artist_id, display_name, folder_name, source_platform
        ) VALUES (?, ?, ?, ?)
        """,
        (artist_id, artist_id, artist_id, source_platform),
    )
    connection.execute(
        """
        INSERT INTO artworks (
            artwork_id, artist_id, raw_path, processed_path, source_platform,
            source_id, is_sfw, demo_eligible, validated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1)
        """,
        (
            artwork_id,
            artist_id,
            f"data/raw/{artwork_id}.jpg",
            f"data/processed/{artwork_id}.jpg",
            source_platform,
            f"candidate-{artwork_id}",
            None if is_sfw is None else int(is_sfw),
        ),
    )
    connection.commit()
    upsert_embedding(
        connection,
        artwork_id,
        ImageEmbeddings(
            clip_vector=np.asarray(clip, dtype=np.float32),
            dino_pooled=np.asarray(dino, dtype=np.float32),
            dino_patches=patches if patches is not None else _patches([1, 0]),
            dino_patch_grid_size=2,
        ),
        app.models,
    )
    if decision is not None:
        _add_decision(connection, artwork_id, decision)


def _replace_embedding(
    connection: sqlite3.Connection,
    app: AppConfig,
    artwork_id: str,
    clip: list[float],
    dino: list[float],
) -> None:
    upsert_embedding(
        connection,
        artwork_id,
        ImageEmbeddings(
            clip_vector=np.asarray(clip, dtype=np.float32),
            dino_pooled=np.asarray(dino, dtype=np.float32),
            dino_patches=_patches([1, 0]),
            dino_patch_grid_size=2,
        ),
        app.models,
    )


def _add_decision(
    connection: sqlite3.Connection,
    artwork_id: str,
    decision: str,
) -> None:
    accepted = int(decision == "accept")
    digest = hashlib.sha256(artwork_id.encode()).hexdigest()
    connection.execute(
        """
        INSERT INTO artwork_filter_decisions (
            decision_key, candidate_id, image_sha256, decision, predicted_class,
            accepted_for_main_corpus, route, final_score, confidence,
            reason_codes_json, candidate_json, evidence_json, model_id,
            config_version, config_hash, software_version, processed_at, duration_ms
        ) VALUES (?, ?, ?, ?, 'finished_illustration', ?, ?, 0.9, 0.9,
                  '[]', '{}', '{}', 'model', 'config', 'hash', 'software',
                  '2026-08-13T00:00:00Z', 1.0)
        """,
        (
            f"decision-{artwork_id}",
            f"candidate-{artwork_id}",
            digest,
            decision,
            accepted,
            "main_art" if accepted else "review",
        ),
    )
    connection.execute(
        """
        INSERT INTO artwork_filter_routes (
            route_key, decision_key, candidate_id, target, status, artwork_id
        ) VALUES (?, ?, ?, ?, 'stored', ?)
        """,
        (
            f"route-{artwork_id}",
            f"decision-{artwork_id}",
            f"candidate-{artwork_id}",
            "corpus" if accepted else "review",
            artwork_id,
        ),
    )
    connection.commit()


def _patches(vector: list[float]) -> np.ndarray:
    return np.tile(np.asarray([vector], dtype=np.float32), (4, 1))
