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
    # Schema-level indexes may reference columns introduced by a migration.
    # Add those columns before executing the current idempotent schema on an
    # existing database; a new database gets them from CREATE TABLE below.
    if _table_exists(conn, "artworks"):
        _ensure_column(
            conn,
            "artworks",
            "demo_eligible",
            "INTEGER NOT NULL DEFAULT 0 CHECK (demo_eligible IN (0, 1))",
        )
    if _table_exists(conn, "embeddings"):
        _ensure_column(conn, "embeddings", "csd_style", "BLOB")
        _ensure_column(conn, "embeddings", "csd_style_dim", "INTEGER")
        _ensure_column(conn, "embeddings", "model_name_csd", "TEXT")
        _ensure_column(conn, "embeddings", "model_version_csd", "TEXT")
        _remove_legacy_dino_embedding_columns(conn)
    if _table_exists(conn, "bluesky_actor_intake_state"):
        _ensure_column(
            conn,
            "bluesky_actor_intake_state",
            "pending_high_water_post_uri",
            "TEXT",
        )
        _ensure_column(conn, "bluesky_actor_intake_state", "resume_cursor", "TEXT")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _ensure_column(conn, "artists", "source_platform", "TEXT DEFAULT 'manual'")
    _ensure_column(
        conn,
        "artworks",
        "demo_eligible",
        "INTEGER NOT NULL DEFAULT 0 CHECK (demo_eligible IN (0, 1))",
    )
    conn.commit()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def _remove_legacy_dino_embedding_columns(conn: sqlite3.Connection) -> None:
    """Rebuild legacy embedding tables so retired patch BLOBs cannot linger."""

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(embeddings)")}
    if not any(
        name.startswith("dino_")
        or name.startswith("model_name_dino")
        or name.startswith("model_version_dino")
        for name in columns
    ):
        return
    conn.execute("DROP TABLE IF EXISTS embeddings_csd_migration")
    conn.execute(
        """
        CREATE TABLE embeddings_csd_migration (
            artwork_id         TEXT PRIMARY KEY REFERENCES artworks(artwork_id),
            clip_vector        BLOB,
            clip_dim           INTEGER,
            csd_style          BLOB,
            csd_style_dim      INTEGER,
            model_name_clip    TEXT,
            model_version_clip TEXT,
            model_name_csd     TEXT,
            model_version_csd  TEXT,
            date_computed      TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        INSERT INTO embeddings_csd_migration (
            artwork_id, clip_vector, clip_dim, csd_style, csd_style_dim,
            model_name_clip, model_version_clip, model_name_csd,
            model_version_csd, date_computed
        )
        SELECT artwork_id, clip_vector, clip_dim, csd_style, csd_style_dim,
               model_name_clip, model_version_clip, model_name_csd,
               model_version_csd, date_computed
          FROM embeddings
        """
    )
    conn.execute("DROP TABLE embeddings")
    conn.execute("ALTER TABLE embeddings_csd_migration RENAME TO embeddings")


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
