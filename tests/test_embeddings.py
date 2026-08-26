from pathlib import Path

import numpy as np
import pytest

from artsearch.embed.pipeline import generate_embeddings_for_config
from artsearch.embed.models import _from_pretrained_cache_first
from artsearch.embed.storage import (
    ImageEmbeddings,
    blob_to_vector,
    l2_normalize,
    upsert_embedding,
)
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


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def embed_images(self, image_paths):
        self.calls += 1
        return [
            ImageEmbeddings(
                clip_vector=np.array([3.0, 4.0], dtype=np.float32),
                csd_style=np.array([1.0, 2.0, 2.0], dtype=np.float32),
            )
            for _ in image_paths
        ]


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
        ),
    )


def test_l2_normalize_handles_vectors_and_rows():
    vector = l2_normalize(np.array([3.0, 4.0], dtype=np.float32))
    rows = l2_normalize(
        np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32),
        axis=1,
    )

    assert vector == pytest.approx(np.array([0.6, 0.8], dtype=np.float32))
    assert rows[0] == pytest.approx(np.array([0.6, 0.8], dtype=np.float32))
    assert rows[1] == pytest.approx(np.array([0.0, 0.0], dtype=np.float32))


def test_model_loader_prefers_pinned_local_cache_then_downloads():
    calls: list[bool] = []

    def loader(model_name: str, *, revision: str, local_files_only: bool = False):
        assert model_name == "example/model"
        assert revision == "revision"
        calls.append(local_files_only)
        if local_files_only:
            raise OSError("not cached")
        return "downloaded"

    loaded = _from_pretrained_cache_first(
        loader,
        "example/model",
        revision="revision",
    )

    assert loaded == "downloaded"
    assert calls == [True, False]


def test_upsert_embedding_stores_normalized_float32_blobs(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    _insert_valid_artwork(conn, config, "art_1", "artist_1")

    embedding = FakeEmbeddingProvider().embed_images([Path("image.jpg")])[0]
    upsert_embedding(conn, "art_1", embedding, config.models)

    row = conn.execute("SELECT * FROM embeddings WHERE artwork_id = 'art_1'").fetchone()
    clip = blob_to_vector(row["clip_vector"], row["clip_dim"])
    csd_style = blob_to_vector(row["csd_style"], row["csd_style_dim"])

    assert clip == pytest.approx(np.array([0.6, 0.8], dtype=np.float32))
    assert csd_style == pytest.approx(np.array([1 / 3, 2 / 3, 2 / 3], dtype=np.float32))
    assert row["model_name_csd"] == "csd-model"
    assert row["model_version_csd"] == "v1"


def test_generate_embeddings_skips_current_model_versions(tmp_path):
    config = _config(tmp_path)
    processed_path = config.processed_dir / "artist_1" / "art_1.jpg"
    processed_path.parent.mkdir(parents=True)
    processed_path.write_bytes(b"placeholder")
    conn = connect(config.database_path)
    init_db(conn)
    _insert_valid_artwork(conn, config, "art_1", "artist_1")
    provider = FakeEmbeddingProvider()

    first = generate_embeddings_for_config(conn, config, provider)
    second = generate_embeddings_for_config(conn, config, provider)

    assert first == {"processed": 1, "skipped": 0, "errors": 0}
    assert second == {"processed": 0, "skipped": 1, "errors": 0}
    assert provider.calls == 1


def test_init_db_rebuilds_legacy_dino_embedding_table(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    _insert_valid_artwork(conn, config, "art_1", "artist_1")
    conn.execute("DROP INDEX IF EXISTS idx_embeddings_csd_models")
    conn.execute("DROP TABLE embeddings")
    conn.execute(
        """
        CREATE TABLE embeddings (
            artwork_id TEXT PRIMARY KEY REFERENCES artworks(artwork_id),
            clip_vector BLOB,
            clip_dim INTEGER,
            dino_pooled BLOB,
            dino_pooled_dim INTEGER,
            dino_patches BLOB,
            dino_patch_grid_size INTEGER,
            dino_patch_dim INTEGER,
            model_name_clip TEXT,
            model_version_clip TEXT,
            model_name_dino TEXT,
            model_version_dino TEXT,
            date_computed TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO embeddings (
            artwork_id, clip_vector, clip_dim, dino_pooled, dino_pooled_dim,
            dino_patches, dino_patch_grid_size, dino_patch_dim,
            model_name_clip, model_version_clip, model_name_dino,
            model_version_dino, date_computed
        ) VALUES ('art_1', X'0000803F00000000', 2, X'00', 1,
                  X'00', 1, 1, 'clip-model', 'v1', 'dino-model', 'v1', 'now')
        """
    )
    conn.commit()

    init_db(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(embeddings)")}
    row = conn.execute("SELECT * FROM embeddings WHERE artwork_id = 'art_1'").fetchone()
    assert not any("dino" in name for name in columns)
    assert row["clip_dim"] == 2
    assert row["model_name_clip"] == "clip-model"
    assert row["csd_style"] is None


def _insert_valid_artwork(
    conn,
    config: AppConfig,
    artwork_id: str,
    artist_id: str,
) -> None:
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
