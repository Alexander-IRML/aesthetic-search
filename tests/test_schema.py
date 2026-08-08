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

    row = conn.execute("SELECT is_sfw FROM artworks WHERE artwork_id = ?", ("art_1",)).fetchone()
    assert row["is_sfw"] is None
