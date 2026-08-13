from artsearch.ingest.artists import ArtistRecord, register_artist
from artsearch.ingest.db import connect, init_db


def test_schema_initializes_and_registers_artist(tmp_path):
    conn = connect(tmp_path / "artsearch.db")
    init_db(conn)

    register_artist(
        conn,
        ArtistRecord(
            artist_id="artist_1",
            display_name="Artist One",
            folder_name="artist_one",
            source_url="https://example.com/artist_one",
        ),
    )

    row = conn.execute("SELECT * FROM artists WHERE artist_id = ?", ("artist_1",)).fetchone()

    assert row["display_name"] == "Artist One"
    assert row["folder_name"] == "artist_one"
    assert row["source_platform"] == "manual"


def test_unlabeled_artwork_safety_state_defaults_to_unknown(tmp_path):
    conn = connect(tmp_path / "artsearch.db")
    init_db(conn)
    register_artist(
        conn,
        ArtistRecord(
            artist_id="artist_1",
            display_name="Artist One",
            folder_name="artist_one",
        ),
    )

    conn.execute(
        """
        INSERT INTO artworks (artwork_id, artist_id, raw_path)
        VALUES (?, ?, ?)
        """,
        ("art_1", "artist_1", "raw/artist_one/piece.jpg"),
    )

    row = conn.execute(
        "SELECT is_sfw, demo_eligible FROM artworks WHERE artwork_id = ?",
        ("art_1",),
    ).fetchone()
    assert row["is_sfw"] is None
    assert row["demo_eligible"] == 0


def test_existing_database_adds_demo_policy_before_creating_its_index(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.executescript(
        """
        CREATE TABLE artists (
            artist_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            folder_name TEXT NOT NULL UNIQUE,
            source_platform TEXT DEFAULT 'manual',
            source_url TEXT,
            notes TEXT,
            date_added TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE artworks (
            artwork_id TEXT PRIMARY KEY,
            artist_id TEXT NOT NULL REFERENCES artists(artist_id),
            raw_path TEXT NOT NULL,
            processed_path TEXT,
            source_platform TEXT DEFAULT 'manual',
            source_id TEXT,
            orig_width INTEGER,
            orig_height INTEGER,
            file_hash TEXT,
            phash TEXT,
            is_sfw INTEGER,
            validated INTEGER DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'unreviewed',
            duplicate_of TEXT REFERENCES artworks(artwork_id),
            scale_factor REAL,
            pad_left INTEGER DEFAULT 0,
            pad_top INTEGER DEFAULT 0,
            pad_right INTEGER DEFAULT 0,
            pad_bottom INTEGER DEFAULT 0,
            crop_left INTEGER DEFAULT 0,
            crop_top INTEGER DEFAULT 0,
            crop_right INTEGER DEFAULT 0,
            crop_bottom INTEGER DEFAULT 0,
            date_added TEXT DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            UNIQUE(source_platform, source_id)
        );
        """
    )

    init_db(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(artworks)")}
    indexes = {row["name"] for row in conn.execute("PRAGMA index_list(artworks)")}
    assert "demo_eligible" in columns
    assert "idx_artworks_demo_policy" in indexes
