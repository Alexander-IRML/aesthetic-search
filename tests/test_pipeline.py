from types import SimpleNamespace

from artsearch.ingest import pipeline
from artsearch.ingest.artists import ArtistRecord, register_artist
from artsearch.ingest.config import (
    AppConfig,
    DuplicateConfig,
    EmbeddingConfig,
    ImageConfig,
    ModelConfig,
    RetrievalConfig,
)
from artsearch.ingest.db import connect, init_db, insert_artwork, log_event, start_run
from artsearch.ingest.transforms import TransformPlan


def _config(tmp_path):
    return AppConfig(
        root_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        processed_dir=tmp_path / "custom_processed",
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


def _artist():
    return ArtistRecord(
        artist_id="artist_1",
        display_name="Artist One",
        folder_name="artist_one",
    )


def _transform():
    return TransformPlan(
        original_width=10,
        original_height=20,
        crop_left=0,
        crop_top=0,
        crop_right=0,
        crop_bottom=0,
        cropped_width=10,
        cropped_height=20,
        scale_factor=22.4,
        resized_width=224,
        resized_height=448,
        pad_left=112,
        pad_top=0,
        pad_right=112,
        pad_bottom=0,
        output_width=448,
        output_height=448,
    )


def test_processed_path_uses_configured_processed_dir(tmp_path):
    config = _config(tmp_path)
    raw_path = config.raw_dir / "artist_one" / "piece.png"

    processed_path = pipeline._processed_path(config, raw_path)

    assert processed_path == config.processed_dir / "artist_one" / "piece.jpg"
    assert pipeline._path_for_db(config, processed_path) == (
        "custom_processed/artist_one/piece.jpg"
    )


def test_existing_raw_path_with_changed_hash_is_reprocessed(tmp_path, monkeypatch):
    config = _config(tmp_path)
    artist = _artist()
    raw_path = config.raw_dir / artist.folder_name / "piece.jpg"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"new image bytes")

    conn = connect(config.database_path)
    init_db(conn)
    register_artist(conn, artist)
    insert_artwork(
        conn,
        {
            "artwork_id": "art_1",
            "artist_id": artist.artist_id,
            "raw_path": pipeline._path_for_db(config, raw_path),
            "processed_path": "custom_processed/artist_one/piece.jpg",
            "file_hash": "old-hash",
            "phash": "old-phash",
            "validated": 1,
        },
    )
    run_id = start_run(conn, "test")

    monkeypatch.setattr(pipeline, "audit_image_file", lambda path: SimpleNamespace(ok=True))
    monkeypatch.setattr(pipeline, "sha256_file", lambda path: "new-hash")
    monkeypatch.setattr(pipeline, "perceptual_hash", lambda path: "new-phash")
    monkeypatch.setattr(pipeline, "phash_distance", lambda left, right: 64)
    monkeypatch.setattr(
        pipeline,
        "standardize_image",
        lambda raw, processed, image_config: SimpleNamespace(
            orig_width=10,
            orig_height=20,
            transform=_transform(),
        ),
    )

    result = pipeline._process_one(conn, run_id, config, artist.artist_id, raw_path)

    row = conn.execute("SELECT * FROM artworks WHERE artwork_id = 'art_1'").fetchone()
    assert result == {"processed": 1, "skipped": 0, "errors": 0}
    assert row["file_hash"] == "new-hash"
    assert row["validated"] == 1
    assert row["processed_path"] == "custom_processed/artist_one/piece.jpg"


def test_near_duplicate_scan_excludes_current_artwork(tmp_path, monkeypatch):
    config = _config(tmp_path)
    conn = connect(config.database_path)
    init_db(conn)
    register_artist(conn, _artist())
    insert_artwork(
        conn,
        {
            "artwork_id": "art_1",
            "artist_id": "artist_1",
            "raw_path": "raw/artist_one/piece.jpg",
            "phash": "same-phash",
        },
    )
    monkeypatch.setattr(pipeline, "phash_distance", lambda left, right: 0)

    duplicate_of = pipeline._find_near_duplicate(
        conn,
        "same-phash",
        6,
        exclude_artwork_id="art_1",
    )

    assert duplicate_of is None


def test_standardize_corpus_logs_image_failure_and_continues(tmp_path, monkeypatch):
    config = _config(tmp_path)
    artist = _artist()
    raw_path = config.raw_dir / artist.folder_name / "piece.jpg"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"image bytes")

    monkeypatch.setattr(pipeline, "load_config", lambda path: config)
    monkeypatch.setattr(pipeline, "load_artist_manifest", lambda path: [artist])
    monkeypatch.setattr(pipeline, "audit_image_file", lambda path: SimpleNamespace(ok=True))
    monkeypatch.setattr(pipeline, "sha256_file", lambda path: "hash")
    monkeypatch.setattr(pipeline, "perceptual_hash", lambda path: "phash")
    monkeypatch.setattr(
        pipeline,
        "standardize_image",
        lambda raw, processed, image_config: (_ for _ in ()).throw(
            RuntimeError("save failed")
        ),
    )

    result = pipeline.standardize_corpus("unused-config.yaml", "unused-artists.yaml")

    conn = connect(config.database_path)
    event = conn.execute(
        "SELECT * FROM run_events WHERE event_type = 'standardization_failed'"
    ).fetchone()
    row = conn.execute(
        "SELECT * FROM artworks WHERE raw_path = ?",
        ("raw/artist_one/piece.jpg",),
    ).fetchone()
    assert result == {"processed": 0, "skipped": 0, "errors": 1}
    assert event["message"] == "save failed"
    assert row["validated"] == 0


def test_metadata_audit_invalidates_missing_processed_file(tmp_path):
    config = _config(tmp_path)
    artist = _artist()
    raw_path = config.raw_dir / artist.folder_name / "piece.jpg"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"image bytes")

    conn = connect(config.database_path)
    init_db(conn)
    register_artist(conn, artist)
    run_id = start_run(conn, "test")
    insert_artwork(
        conn,
        {
            "artwork_id": "art_1",
            "artist_id": artist.artist_id,
            "raw_path": "raw/artist_one/piece.jpg",
            "processed_path": "custom_processed/artist_one/piece.jpg",
            "file_hash": "hash",
            "phash": "phash",
            "validated": 1,
        },
    )

    errors = pipeline._audit_corpus_metadata(conn, run_id, config, [artist])

    row = conn.execute("SELECT validated FROM artworks WHERE artwork_id = 'art_1'").fetchone()
    assert errors == 1
    assert row["validated"] == 0


def test_metadata_audit_does_not_double_count_known_failed_raw_file(tmp_path):
    config = _config(tmp_path)
    artist = _artist()
    raw_path = config.raw_dir / artist.folder_name / "bad.jpg"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"not really an image")

    conn = connect(config.database_path)
    init_db(conn)
    register_artist(conn, artist)
    run_id = start_run(conn, "test")
    log_event(
        conn,
        run_id,
        level="error",
        event_type="audit_failed",
        raw_path="raw/artist_one/bad.jpg",
        message="cannot identify image file",
    )

    errors = pipeline._audit_corpus_metadata(conn, run_id, config, [artist])

    assert errors == 0
