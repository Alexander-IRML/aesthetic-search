from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def start_run(conn: sqlite3.Connection, script_name: str, notes: str | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (script_name, notes) VALUES (?, ?)",
        (script_name, notes),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    images_processed: int,
    images_skipped: int,
    errors_count: int,
) -> None:
    conn.execute(
        """
        UPDATE runs
           SET finished_at = CURRENT_TIMESTAMP,
               images_processed = ?,
               images_skipped = ?,
               errors_count = ?
         WHERE run_id = ?
        """,
        (images_processed, images_skipped, errors_count, run_id),
    )
    conn.commit()


def log_event(
    conn: sqlite3.Connection,
    run_id: int | None,
    *,
    level: str,
    event_type: str,
    message: str,
    raw_path: str | None = None,
    artwork_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO run_events (run_id, level, event_type, raw_path, artwork_id, message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, level, event_type, raw_path, artwork_id, message),
    )
    conn.commit()


def find_validated_by_hash(
    conn: sqlite3.Connection,
    file_hash: str,
    *,
    exclude_artwork_id: str | None = None,
) -> sqlite3.Row | None:
    if exclude_artwork_id is None:
        return conn.execute(
            """
            SELECT *
              FROM artworks
             WHERE file_hash = ?
               AND validated = 1
             LIMIT 1
            """,
            (file_hash,),
        ).fetchone()

    return conn.execute(
        """
        SELECT *
          FROM artworks
         WHERE file_hash = ?
           AND validated = 1
           AND artwork_id != ?
         LIMIT 1
        """,
        (file_hash, exclude_artwork_id),
    ).fetchone()


def find_artwork_by_raw_path(conn: sqlite3.Connection, raw_path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM artworks WHERE raw_path = ? LIMIT 1",
        (raw_path,),
    ).fetchone()


def insert_artwork(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO artworks ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
    conn.commit()


def update_artwork_standardization(
    conn: sqlite3.Connection,
    artwork_id: str,
    values: dict[str, Any],
) -> None:
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn.execute(
        f"UPDATE artworks SET {assignments} WHERE artwork_id = ?",
        (*values.values(), artwork_id),
    )
    conn.commit()
