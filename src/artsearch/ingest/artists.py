from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ArtistRecord:
    artist_id: str
    display_name: str
    folder_name: str
    source_platform: str = "manual"
    source_url: str | None = None
    notes: str | None = None


def load_artist_manifest(path: str | Path = "config/artists.yaml") -> list[ArtistRecord]:
    manifest_path = Path(path)
    with manifest_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    artists = []
    for row in raw.get("artists", []):
        artists.append(
            ArtistRecord(
                artist_id=str(row["artist_id"]),
                display_name=str(row["display_name"]),
                folder_name=str(row["folder_name"]),
                source_platform=str(row.get("source_platform", "manual")),
                source_url=row.get("source_url"),
                notes=row.get("notes"),
            )
        )
    return artists


def register_artist(conn: sqlite3.Connection, artist: ArtistRecord) -> None:
    conn.execute(
        """
        INSERT INTO artists (
            artist_id,
            display_name,
            folder_name,
            source_platform,
            source_url,
            notes
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(artist_id) DO UPDATE SET
            display_name = excluded.display_name,
            folder_name = excluded.folder_name,
            source_platform = excluded.source_platform,
            source_url = excluded.source_url,
            notes = excluded.notes
        """,
        (
            artist.artist_id,
            artist.display_name,
            artist.folder_name,
            artist.source_platform,
            artist.source_url,
            artist.notes,
        ),
    )
def register_artists(conn: sqlite3.Connection, artists: list[ArtistRecord]) -> int:
    for artist in artists:
        register_artist(conn, artist)
    return len(artists)
