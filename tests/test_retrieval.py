import json
from pathlib import Path

import numpy as np

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
from artsearch.retrieval.demo import write_gallery_demo, write_search_demo
from artsearch.retrieval.search import RetrievalMode, SearchFilters, search_similar


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
            csd_model_name="csd-model",
            csd_model_version="v1",
            csd_model_filename="model.pt",
            csd_model_sha256="a" * 64,
            csd_processor_name="clip-processor",
            csd_processor_version="v1",
        ),
        embeddings=EmbeddingConfig(batch_size=2, device="cpu"),
        retrieval=RetrievalConfig(
            default_top_k=5,
            demo_output_path=tmp_path / "search_demo.html",
            gallery_output_path=tmp_path / "search_gallery.html",
            shortlist_size=2,
        ),
    )


def test_search_filters_same_artist_before_truncating(tmp_path):
    config = _config(tmp_path)
    connection = connect(config.database_path)
    init_db(connection)
    _insert(connection, config, "query", "artist_a", [1, 0], [1, 0])
    _insert(connection, config, "same", "artist_a", [0.99, 0.01], [1, 0])
    _insert(connection, config, "other", "artist_b", [0.9, 0.1], [0.9, 0.1])

    results = search_similar(connection, config, "query", top_k=1)

    assert [result.artwork_id for result in results] == ["other"]


def test_component_modes_rank_independently(tmp_path):
    config = _config(tmp_path)
    connection = connect(config.database_path)
    init_db(connection)
    _insert(connection, config, "query", "artist_a", [1, 0], [1, 0])
    _insert(connection, config, "subject", "artist_b", [1, 0], [0, 1])
    _insert(connection, config, "style", "artist_c", [0, 1], [1, 0])

    clip = search_similar(connection, config, "query", top_k=1, mode=RetrievalMode.CLIP_SUBJECT)
    style = search_similar(connection, config, "query", top_k=1, mode=RetrievalMode.CSD_STYLE)

    assert clip[0].artwork_id == "subject"
    assert clip[0].clip_rank == 1
    assert style[0].artwork_id == "style"
    assert style[0].style_rank == 1


def test_ensemble_fuses_clip_and_csd_rankings_with_auditable_evidence(tmp_path):
    config = _config(tmp_path)
    connection = connect(config.database_path)
    init_db(connection)
    _insert(connection, config, "query", "artist_a", [1, 0], [1, 0])
    _insert(connection, config, "subject", "artist_b", [1, 0], [0, 1])
    _insert(connection, config, "style", "artist_c", [0, 1], [1, 0])
    _insert(connection, config, "balanced", "artist_d", [0.8, 0.2], [0.8, 0.2])

    results = search_similar(connection, config, "query", top_k=3, mode=RetrievalMode.ENSEMBLE)

    assert {result.artwork_id for result in results} == {"subject", "style", "balanced"}
    assert all(result.fusion_rank is not None for result in results)
    assert all(result.clip_rank is not None for result in results)
    assert all(result.style_rank is not None for result in results)
    assert all(result.score == result.fusion_score for result in results)
    assert all(result.shortlist_size == 3 for result in results)
    assert all(result.candidate_count == 3 for result in results)


def test_search_filters_review_status_and_sfw_candidates(tmp_path):
    config = _config(tmp_path)
    connection = connect(config.database_path)
    init_db(connection)
    _insert(connection, config, "query", "artist_a", [1, 0], [1, 0])
    _insert(
        connection,
        config,
        "unsafe",
        "artist_b",
        [0.99, 0.01],
        [0.99, 0.01],
        is_sfw=False,
        review_status="confirmed_unique",
    )
    _insert(
        connection,
        config,
        "safe",
        "artist_c",
        [0.9, 0.1],
        [0.9, 0.1],
        review_status="confirmed_variant",
    )

    results = search_similar(
        connection,
        config,
        "query",
        filters=SearchFilters(
            top_k=5,
            is_sfw=True,
            review_status="confirmed_variant",
        ),
    )

    assert [result.artwork_id for result in results] == ["safe"]


def test_search_demo_writes_current_model_modes(tmp_path, monkeypatch):
    config = _config(tmp_path)
    connection = connect(config.database_path)
    init_db(connection)
    _insert(connection, config, "query", "artist_a", [1, 0], [1, 0])
    _insert(connection, config, "result", "artist_b", [0.8, 0.2], [0.7, 0.3])
    monkeypatch.setattr("artsearch.retrieval.demo.load_config", lambda path: config)

    output_path = write_search_demo("query", config_path="unused.yaml")

    html = output_path.read_text(encoding="utf-8")
    assert "ArtSearch Search Demo" in html
    assert "CLIP subject" in html
    assert "CSD style" in html
    assert "processed/artist_a/query.jpg" in html


def test_gallery_demo_exposes_fusion_and_component_evidence(tmp_path, monkeypatch):
    config = _config(tmp_path)
    connection = connect(config.database_path)
    init_db(connection)
    _insert(connection, config, "query", "artist_a", [1, 0], [1, 0])
    _insert(connection, config, "result", "artist_b", [0.8, 0.2], [0.7, 0.3])
    monkeypatch.setattr("artsearch.retrieval.demo.load_config", lambda path: config)

    output_path = write_gallery_demo(
        config_path="unused.yaml",
        sample_per_artist=1,
        top_k=1,
    )

    html = output_path.read_text(encoding="utf-8")
    payload = _dashboard_payload(output_path)
    first_result = payload["queries"][0]["results"][0]
    first_session = payload["evaluation"]["sessions"][0]
    assert "ArtSearch Retrieval Workbench" in html
    assert "CSD style" in html
    assert "Multi-retriever fusion diagnostics" in html
    assert payload["schemaVersion"] == "4.0"
    assert payload["mode"]["value"] == RetrievalMode.ENSEMBLE.value
    assert first_result["retrieval"]["orderingSignal"] == "fusion"
    assert set(first_result["retrieval"]["components"]) == {"fusion", "clip", "style"}
    assert first_session["queryCount"] == 2
    assert payload["evaluation"]["funnelStats"]["meanClipTopKAgreement"] == 1.0
    assert payload["evaluation"]["funnelStats"]["meanStyleTopKAgreement"] == 1.0


def test_gallery_sessions_are_reproducible_and_nonrepeating(tmp_path, monkeypatch):
    config = _config(tmp_path)
    connection = connect(config.database_path)
    init_db(connection)
    for artist_index, artist_id in enumerate(("artist_a", "artist_b")):
        for artwork_index in range(3):
            vector = [1.0 + artist_index, 0.1 + artwork_index]
            _insert(
                connection,
                config,
                f"{artist_id}_{artwork_index}",
                artist_id,
                vector,
                vector,
            )
    monkeypatch.setattr("artsearch.retrieval.demo.load_config", lambda path: config)

    first = write_gallery_demo(
        config_path="unused.yaml",
        output_path=tmp_path / "first.html",
        sample_per_artist=2,
        review_session_count=3,
        review_seed="repeatable-review",
        top_k=1,
    )
    second = write_gallery_demo(
        config_path="unused.yaml",
        output_path=tmp_path / "second.html",
        sample_per_artist=2,
        review_session_count=3,
        review_seed="repeatable-review",
        top_k=1,
    )

    first_sessions = _dashboard_payload(first)["evaluation"]["sessions"]
    second_sessions = _dashboard_payload(second)["evaluation"]["sessions"]
    flattened = [entry["query"] for session in first_sessions for entry in session["queries"]]
    assert [session["queryCount"] for session in first_sessions] == [2, 2, 2]
    assert len({query["artworkId"] for query in flattened}) == 6
    assert [session["id"] for session in first_sessions] == [
        session["id"] for session in second_sessions
    ]


def _dashboard_payload(path: Path) -> dict:
    html = path.read_text(encoding="utf-8")
    embedded = html.split('<script id="searchData" type="application/json">', maxsplit=1)[1].split(
        "</script>", maxsplit=1
    )[0]
    return json.loads(embedded)


def _insert(
    connection,
    config: AppConfig,
    artwork_id: str,
    artist_id: str,
    clip_vector: list[float],
    csd_style: list[float],
    *,
    is_sfw: bool = True,
    review_status: str = "unreviewed",
) -> None:
    processed_path = config.processed_dir / artist_id / f"{artwork_id}.jpg"
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    processed_path.write_bytes(b"placeholder")
    register_artist(
        connection,
        ArtistRecord(
            artist_id=artist_id,
            display_name=artist_id,
            folder_name=artist_id,
        ),
    )
    insert_artwork(
        connection,
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
    upsert_embedding(
        connection,
        artwork_id,
        ImageEmbeddings(
            clip_vector=np.asarray(clip_vector, dtype=np.float32),
            csd_style=np.asarray(csd_style, dtype=np.float32),
        ),
        config.models,
    )
