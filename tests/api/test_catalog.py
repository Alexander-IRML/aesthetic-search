import asyncio
import json
from dataclasses import replace
from pathlib import Path

from PIL import Image

from artsearch.api.catalog import SQLiteArtworkCatalog, _source_media_url
from artsearch.api.config import SearchApiConfig
from artsearch.ingest.config import load_config
from artsearch.ingest.db import connect, init_db


ROOT = Path(__file__).resolve().parents[2]


def test_sqlite_catalog_returns_controlled_image_route(tmp_path: Path) -> None:
    base = load_config(ROOT / "config/config.yaml")
    app = replace(
        base,
        root_dir=tmp_path,
        raw_dir=tmp_path / "data/raw",
        processed_dir=tmp_path / "data/processed",
        database_path=tmp_path / "data/artsearch.db",
    )
    image_path = app.processed_dir / "artist" / "art.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), "white").save(image_path)
    with connect(app.database_path) as connection:
        init_db(connection)
        connection.execute(
            """
            INSERT INTO artists (artist_id, display_name, folder_name, source_url)
            VALUES ('artist', 'Artist Name', 'artist', 'https://bsky.app/profile/artist')
            """
        )
        connection.execute(
            """
            INSERT INTO artworks (
                artwork_id, artist_id, raw_path, processed_path,
                is_sfw, demo_eligible, validated
            ) VALUES ('art', 'artist', 'raw.jpg', ?, 1, 1, 1)
            """,
            (str(image_path.relative_to(tmp_path)),),
        )
        connection.commit()

    catalog = SQLiteArtworkCatalog(app, SearchApiConfig())
    records = asyncio.run(catalog.get_many(["art"]))
    record = records["art"]

    assert record.artist_name == "Artist Name"
    assert record.image_url == "/api/artworks/art/image"
    assert record.post_text is None
    assert record.alt_text is None
    assert asyncio.run(catalog.local_image_path("art")) == image_path
    assert asyncio.run(catalog.local_image_path("missing")) is None


def test_sqlite_catalog_falls_back_to_trusted_bluesky_media(tmp_path: Path) -> None:
    base = load_config(ROOT / "config/config.yaml")
    app = replace(
        base,
        root_dir=tmp_path,
        raw_dir=tmp_path / "data/raw",
        processed_dir=tmp_path / "data/processed",
        database_path=tmp_path / "data/artsearch.db",
    )
    media_url = "https://cdn.bsky.app/img/feed_fullsize/plain/did:plc:test/blob@jpeg"
    with connect(app.database_path) as connection:
        init_db(connection)
        connection.execute(
            """
            INSERT INTO artists (artist_id, display_name, folder_name, source_platform)
            VALUES ('artist', 'Artist Name', 'artist', 'bluesky')
            """
        )
        connection.execute(
            """
            INSERT INTO artworks (
                artwork_id, artist_id, raw_path, processed_path, source_platform,
                source_id, is_sfw, demo_eligible, validated
            ) VALUES ('art', 'artist', 'raw.jpg', 'processed.jpg', 'bluesky',
                      'candidate', 1, 1, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO artwork_filter_decisions (
                decision_key, candidate_id, decision, predicted_class,
                accepted_for_main_corpus, route, final_score, confidence,
                reason_codes_json, candidate_json, evidence_json, model_id,
                config_version, config_hash, software_version, processed_at,
                duration_ms
            ) VALUES (
                'decision', 'candidate', 'accept', 'finished_illustration',
                1, 'main_art', 0.9, 0.9, '[]', ?, '{}', 'siglip',
                '1', 'hash', 'version', '2026-09-08T00:00:00Z', 1.0
            )
            """,
            (json.dumps({"fullsize_url": media_url}),),
        )
        connection.commit()

    catalog = SQLiteArtworkCatalog(app, SearchApiConfig(serve_local_images=False))

    assert asyncio.run(catalog.get_many(["art"]))["art"].image_url == media_url


def test_source_media_fallback_rejects_untrusted_urls() -> None:
    assert _source_media_url({"fullsize_url": "http://cdn.bsky.app/image.jpg"}) is None
    assert _source_media_url({"fullsize_url": "https://bsky.app.evil.test/image.jpg"}) is None
    assert _source_media_url({"fullsize_url": "https://user@cdn.bsky.app/image.jpg"}) is None
