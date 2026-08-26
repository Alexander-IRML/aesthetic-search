from dataclasses import replace
from pathlib import Path

from PIL import Image

from artsearch.api.catalog import SQLiteArtworkCatalog
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
    record = catalog.get_many(["art"])["art"]

    assert record.artist_name == "Artist Name"
    assert record.image_url == "/api/artworks/art/image"
    assert catalog.local_image_path("art") == image_path
    assert catalog.local_image_path("missing") is None
