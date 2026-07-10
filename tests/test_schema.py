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
