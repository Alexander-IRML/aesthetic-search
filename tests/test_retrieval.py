from dataclasses import replace
import json
from pathlib import Path

import numpy as np

import artsearch.retrieval.search as search_module
from artsearch.embed.storage import ImageEmbeddings, upsert_embedding
from artsearch.ingest.artists import ArtistRecord, register_artist
from artsearch.ingest.config import (
    AppConfig,
    DuplicateConfig,
    EmbeddingConfig,
    ImageConfig,
    ModelConfig,
    RetrievalConfig,
)
from artsearch.ingest.db import connect, init_db, insert_artwork
from artsearch.retrieval.diagnostics import patch_maxsim_diagnostics
from artsearch.retrieval.demo import write_gallery_demo, write_search_demo
from artsearch.retrieval.search import (
    RetrievalMode,
    SearchFilters,
    patch_maxsim_score,
    search_similar,
)


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        root_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        processed_dir=tmp_path / "processed",
        database_path=tmp_path / "artsearch.db",
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
            clip_model_name="clip-model",
            clip_model_version="v1",
            dino_model_name="dino-model",
            dino_model_version="v1",
        ),
        embeddings=EmbeddingConfig(batch_size=2, device="cpu"),
        retrieval=RetrievalConfig(
            default_top_k=5,
            demo_output_path=tmp_path / "search_demo.html",
            gallery_output_path=tmp_path / "search_gallery.html",
        ),
    )


def test_search_filters_same_artist_before_truncating(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="query",
        artist_id="artist_a",
        vector=np.array([1.0, 0.0], dtype=np.float32),
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="same_artist_match",
        artist_id="artist_a",
        vector=np.array([0.99, 0.01], dtype=np.float32),
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="other_artist_match",
        artist_id="artist_b",
        vector=np.array([0.9, 0.1], dtype=np.float32),
    )

    results = search_similar(conn, config, "query", top_k=1)

    assert [result.artwork_id for result in results] == ["other_artist_match"]


def test_search_can_rank_by_clip_subject_mode(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="query",
        artist_id="artist_a",
        clip_vector=np.array([1.0, 0.0], dtype=np.float32),
        dino_pooled=np.array([0.0, 1.0], dtype=np.float32),
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="clip_match",
        artist_id="artist_b",
        clip_vector=np.array([0.95, 0.05], dtype=np.float32),
        dino_pooled=np.array([0.0, 1.0], dtype=np.float32),
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="pooled_match",
        artist_id="artist_c",
        clip_vector=np.array([0.0, 1.0], dtype=np.float32),
        dino_pooled=np.array([0.0, 0.99], dtype=np.float32),
    )

    results = search_similar(
        conn,
        config,
        "query",
        top_k=1,
        mode=RetrievalMode.CLIP_SUBJECT,
    )

    assert [result.artwork_id for result in results] == ["clip_match"]
    assert results[0].mode == RetrievalMode.CLIP_SUBJECT


def test_patch_maxsim_is_spatially_agnostic(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    query_patches = np.array(
        [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]],
        dtype=np.float32,
    )
    rearranged_match = np.array(
        [[0.0, 1.0], [1.0, 0.0], [0.0, 1.0], [1.0, 0.0]],
        dtype=np.float32,
    )
    partial_match = np.array(
        [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0], [1.0, 0.0]],
        dtype=np.float32,
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="query",
        artist_id="artist_a",
        dino_patches=query_patches,
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="rearranged_match",
        artist_id="artist_b",
        dino_patches=rearranged_match,
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="partial_match",
        artist_id="artist_c",
        dino_patches=partial_match,
    )

    assert patch_maxsim_score(query_patches, rearranged_match) == 1.0
    assert patch_maxsim_score(query_patches, partial_match) == 0.5

    results = search_similar(
        conn,
        config,
        "query",
        top_k=2,
        mode=RetrievalMode.DINO_PATCH_MAXSIM,
    )

    assert [result.artwork_id for result in results] == ["rearranged_match", "partial_match"]
    assert results[0].score == 1.0


def test_ensemble_shortlists_by_pooled_then_reranks_by_patch(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config = replace(
        config,
        retrieval=RetrievalConfig(
            default_top_k=2,
            demo_output_path=config.retrieval.demo_output_path,
            gallery_output_path=config.retrieval.gallery_output_path,
            shortlist_size=2,
            patch_match_top_n=1,
        ),
    )
    conn = connect(config.database_path)
    init_db(conn)
    matching_patches = np.tile(np.array([[1.0, 0.0]], dtype=np.float32), (4, 1))
    nonmatching_patches = np.tile(np.array([[0.0, 1.0]], dtype=np.float32), (4, 1))
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="query",
        artist_id="artist_a",
        clip_vector=np.array([1.0, 0.0], dtype=np.float32),
        dino_pooled=np.array([1.0, 0.0], dtype=np.float32),
        dino_patches=matching_patches,
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="clip_and_pooled_winner",
        artist_id="artist_b",
        clip_vector=np.array([1.0, 0.0], dtype=np.float32),
        dino_pooled=np.array([0.99, 0.01], dtype=np.float32),
        dino_patches=nonmatching_patches,
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="patch_winner",
        artist_id="artist_c",
        clip_vector=np.array([-1.0, 0.0], dtype=np.float32),
        dino_pooled=np.array([0.8, 0.2], dtype=np.float32),
        dino_patches=matching_patches,
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="outside_shortlist",
        artist_id="artist_d",
        clip_vector=np.array([0.0, 1.0], dtype=np.float32),
        dino_pooled=np.array([-1.0, 0.0], dtype=np.float32),
        dino_patches=matching_patches,
    )

    loaded_patch_ids = []
    original_loader = search_module._load_patch_matrices

    def tracking_loader(conn, config, artwork_ids):
        loaded_patch_ids.extend(artwork_ids)
        return original_loader(conn, config, artwork_ids)

    monkeypatch.setattr(search_module, "_load_patch_matrices", tracking_loader)

    results = search_similar(
        conn,
        config,
        "query",
        top_k=2,
        mode=RetrievalMode.ENSEMBLE,
    )

    assert [result.artwork_id for result in results] == [
        "patch_winner",
        "clip_and_pooled_winner",
    ]
    assert results[0].pooled_rank == 2
    assert results[0].patch_rank == 1
    assert results[0].clip_rank == 3
    assert results[0].score == results[0].patch_score == 1.0
    assert results[0].shortlist_size == 2
    assert results[0].candidate_count == 3
    assert set(loaded_patch_ids) == {
        "query",
        "clip_and_pooled_winner",
        "patch_winner",
    }
    assert "outside_shortlist" not in loaded_patch_ids


def test_patch_maxsim_can_average_multiple_candidate_matches():
    query = np.array([[1.0, 0.0]], dtype=np.float32)
    candidate = np.array([[1.0, 0.0], [-1.0, 0.0]], dtype=np.float32)

    assert patch_maxsim_score(query, candidate, top_n=1) == 1.0
    assert patch_maxsim_score(query, candidate, top_n=2) == 0.0


def test_search_filters_review_status_and_sfw_candidates(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="query",
        artist_id="artist_a",
        vector=np.array([1.0, 0.0], dtype=np.float32),
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="unsafe_match",
        artist_id="artist_b",
        vector=np.array([0.99, 0.01], dtype=np.float32),
        is_sfw=False,
        review_status="confirmed_unique",
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="safe_variant",
        artist_id="artist_c",
        vector=np.array([0.9, 0.1], dtype=np.float32),
        is_sfw=True,
        review_status="confirmed_variant",
    )

    results = search_similar(
        conn,
        config,
        "query",
        filters=SearchFilters(
            top_k=5,
            is_sfw=True,
            review_status="confirmed_variant",
        ),
    )

    assert [result.artwork_id for result in results] == ["safe_variant"]
    assert results[0].is_sfw is True
    assert results[0].review_status == "confirmed_variant"


def test_patch_diagnostics_reports_best_patch_matches(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="query",
        artist_id="artist_a",
        dino_patches=np.array(
            [[1.0, 0.0], [0.0, 1.0], [0.6, 0.8], [1.0, 0.0]],
            dtype=np.float32,
        ),
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="candidate",
        artist_id="artist_b",
        dino_patches=np.array(
            [[0.0, 1.0], [1.0, 0.0], [0.6, 0.8], [1.0, 0.0]],
            dtype=np.float32,
        ),
    )

    matches = patch_maxsim_diagnostics(
        conn,
        config,
        "query",
        "candidate",
        top_n=2,
    )

    assert len(matches) == 2
    assert matches[0].score == 1.0
    assert matches[0].candidate_patch_index in {1, 2, 3}
    assert matches[0].query_row in {0, 1}
    assert matches[0].query_col in {0, 1}


def test_search_demo_writes_html_with_relative_image_links(tmp_path, monkeypatch):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="query",
        artist_id="artist_a",
        vector=np.array([1.0, 0.0], dtype=np.float32),
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="result",
        artist_id="artist_b",
        vector=np.array([0.8, 0.2], dtype=np.float32),
    )
    monkeypatch.setattr("artsearch.retrieval.demo.load_config", lambda path: config)

    output_path = write_search_demo("query", config_path="unused.yaml")

    html = output_path.read_text(encoding="utf-8")
    assert "ArtSearch Search Demo" in html
    assert "CLIP subject" in html
    assert "DINO patch MaxSim" in html
    assert "processed/artist_a/query.jpg" in html
    assert "processed/artist_b/result.jpg" in html


def test_gallery_demo_writes_clickable_query_payload(tmp_path, monkeypatch):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="query",
        artist_id="artist_a",
        vector=np.array([1.0, 0.0], dtype=np.float32),
    )
    _insert_artwork_with_embedding(
        conn,
        config,
        artwork_id="result",
        artist_id="artist_b",
        vector=np.array([0.8, 0.2], dtype=np.float32),
    )
    monkeypatch.setattr("artsearch.retrieval.demo.load_config", lambda path: config)

    output_path = write_gallery_demo(
        config_path="unused.yaml",
        sample_per_artist=1,
        top_k=1,
    )

    html = output_path.read_text(encoding="utf-8")
    assert "ArtSearch Retrieval Workbench" in html
    assert "Evaluation Stats" in html
    assert "Export judgments" in html
    assert "Next review session" in html
    assert "currentSessionIsExported" in html
    assert "review_session_id" in html
    assert "CLIP subject" in html
    assert "DINO patch MaxSim" in html
    assert "Two-stage funnel diagnostics" in html
    assert "CLIP semantic lens" in html
    assert "processed/artist_a/query.jpg" in html
    assert "processed/artist_b/result.jpg" in html
    assert "data-index" in html
    embedded = html.split(
        '<script id="searchData" type="application/json">',
        maxsplit=1,
    )[1].split("</script>", maxsplit=1)[0]
    payload = json.loads(embedded)
    first_result = payload["queries"][0]["results"][0]
    first_session = payload["evaluation"]["sessions"][0]
    assert payload["schemaVersion"] == "3.0"
    assert payload["mode"]["value"] == RetrievalMode.ENSEMBLE.value
    assert first_result["retrieval"]["orderingSignal"] == "patch"
    assert set(first_result["retrieval"]["components"]) == {"pooled", "patch", "clip"}
    assert first_session["queryCount"] == 2
    assert payload["evaluation"]["sessionPlan"]["requestedSessions"] == 3
    assert payload["evaluation"]["sessionPlan"]["generatedSessions"] == 1
    assert payload["evaluation"]["funnelStats"]["meanPatchTopKAgreement"] == 1.0


def test_gallery_demo_builds_reproducible_nonrepeating_review_sessions(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    for artist_index, artist_id in enumerate(("artist_a", "artist_b")):
        for artwork_index in range(3):
            _insert_artwork_with_embedding(
                conn,
                config,
                artwork_id=f"{artist_id}_{artwork_index}",
                artist_id=artist_id,
                vector=np.array(
                    [1.0 + artist_index, 0.1 + artwork_index],
                    dtype=np.float32,
                ),
            )
    monkeypatch.setattr("artsearch.retrieval.demo.load_config", lambda path: config)

    first_path = write_gallery_demo(
        config_path="unused.yaml",
        output_path=tmp_path / "first.html",
        sample_per_artist=2,
        review_session_count=3,
        review_seed="repeatable-review",
        top_k=1,
    )
    second_path = write_gallery_demo(
        config_path="unused.yaml",
        output_path=tmp_path / "second.html",
        sample_per_artist=2,
        review_session_count=3,
        review_seed="repeatable-review",
        top_k=1,
    )

    first_payload = _dashboard_payload(first_path)
    second_payload = _dashboard_payload(second_path)
    sessions = first_payload["evaluation"]["sessions"]
    flattened_queries = [
        entry["query"]
        for session in sessions
        for entry in session["queries"]
    ]

    assert first_payload["evaluation"]["sessionPlan"] == {
        "seed": "repeatable-review",
        "requestedSessions": 3,
        "generatedSessions": 3,
        "selection": "seeded_artist_balanced_without_replacement",
    }
    assert [session["queryCount"] for session in sessions] == [2, 2, 2]
    assert [session["artistCount"] for session in sessions] == [2, 2, 2]
    assert len({query["artworkId"] for query in flattened_queries}) == 6
    assert [session["id"] for session in sessions] == [
        session["id"] for session in second_payload["evaluation"]["sessions"]
    ]
    assert [
        [entry["query"]["artworkId"] for entry in session["queries"]]
        for session in sessions
    ] == [
        [entry["query"]["artworkId"] for entry in session["queries"]]
        for session in second_payload["evaluation"]["sessions"]
    ]


def _dashboard_payload(path: Path) -> dict:
    html = path.read_text(encoding="utf-8")
    embedded = html.split(
        '<script id="searchData" type="application/json">',
        maxsplit=1,
    )[1].split("</script>", maxsplit=1)[0]
    return json.loads(embedded)


def _insert_artwork_with_embedding(
    conn,
    config: AppConfig,
    *,
    artwork_id: str,
    artist_id: str,
    vector: np.ndarray | None = None,
    clip_vector: np.ndarray | None = None,
    dino_pooled: np.ndarray | None = None,
    dino_patches: np.ndarray | None = None,
    is_sfw: bool = True,
    review_status: str = "unreviewed",
) -> None:
    processed_path = config.processed_dir / artist_id / f"{artwork_id}.jpg"
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    processed_path.write_bytes(b"placeholder")
    register_artist(
        conn,
        ArtistRecord(
            artist_id=artist_id,
            display_name=artist_id,
            folder_name=artist_id,
        ),
    )
    insert_artwork(
        conn,
        {
            "artwork_id": artwork_id,
            "artist_id": artist_id,
            "raw_path": f"raw/{artist_id}/{artwork_id}.jpg",
            "processed_path": f"processed/{artist_id}/{artwork_id}.jpg",
            "validated": 1,
            "is_sfw": int(is_sfw),
            "review_status": review_status,
        },
    )
    pooled = dino_pooled if dino_pooled is not None else vector
    if pooled is None:
        pooled = np.array([1.0, 0.0], dtype=np.float32)
    upsert_embedding(
        conn,
        artwork_id,
        ImageEmbeddings(
            clip_vector=(
                clip_vector
                if clip_vector is not None
                else np.array([1.0, 0.0], dtype=np.float32)
            ),
            dino_pooled=pooled,
            dino_patches=(
                dino_patches
                if dino_patches is not None
                else np.array(
                    [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]],
                    dtype=np.float32,
                )
            ),
            dino_patch_grid_size=2,
        ),
        config.models,
    )
