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
from artsearch.retrieval.search import search_similar


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
    assert "ArtSearch Baseline Demo" in html
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
    assert "ArtSearch Gallery Demo" in html
    assert "processed/artist_a/query.jpg" in html
    assert "processed/artist_b/result.jpg" in html
    assert "data-index" in html


def _insert_artwork_with_embedding(
    conn,
    config: AppConfig,
    *,
    artwork_id: str,
    artist_id: str,
    vector: np.ndarray,
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
        },
    )
    upsert_embedding(
        conn,
        artwork_id,
        ImageEmbeddings(
            clip_vector=np.array([1.0, 0.0], dtype=np.float32),
            dino_pooled=vector,
            dino_patches=np.array(
                [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]],
                dtype=np.float32,
            ),
            dino_patch_grid_size=2,
        ),
        config.models,
    )
