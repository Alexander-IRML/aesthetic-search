from __future__ import annotations

from collections.abc import Iterator
import sqlite3
from pathlib import Path

from artsearch.embed.models import EmbeddingProvider, HuggingFaceEmbeddingProvider
from artsearch.embed.storage import upsert_embedding
from artsearch.ingest.config import AppConfig, load_config
from artsearch.ingest.db import connect, finish_run, init_db, log_event, start_run


def generate_embeddings(
    config_path: str | Path = "config/config.yaml",
    provider: EmbeddingProvider | None = None,
) -> dict[str, int]:
    config = load_config(config_path)
    with connect(config.database_path) as conn:
        init_db(conn)
        return generate_embeddings_for_config(conn, config, provider)


def generate_embeddings_for_config(
    conn: sqlite3.Connection,
    config: AppConfig,
    provider: EmbeddingProvider | None = None,
) -> dict[str, int]:
    model_provider = provider
    run_id = start_run(conn, "generate_embeddings")
    processed = errors = 0
    skipped = _current_embedding_count(conn, config)
    try:
        for batch in _pending_artwork_batches(conn, config):
            work_items = []
            for row in batch:
                processed_path = _path_from_db(config, row["processed_path"])
                if not processed_path.exists():
                    log_event(
                        conn,
                        run_id,
                        level="error",
                        event_type="missing_processed_file",
                        artwork_id=row["artwork_id"],
                        message=f"Processed image does not exist: {row['processed_path']}",
                    )
                    errors += 1
                    continue

                work_items.append((row["artwork_id"], processed_path))

            if not work_items:
                continue

            try:
                if model_provider is None:
                    model_provider = HuggingFaceEmbeddingProvider(config)
                image_paths = [item[1] for item in work_items]
                embeddings = model_provider.embed_images(image_paths)
                if len(embeddings) != len(work_items):
                    raise RuntimeError("Embedding provider returned the wrong result count")
                for (artwork_id, _), embedding in zip(work_items, embeddings, strict=True):
                    upsert_embedding(conn, artwork_id, embedding, config.models)
                    log_event(
                        conn,
                        run_id,
                        level="info",
                        event_type="embedding_computed",
                        artwork_id=artwork_id,
                        message="Computed CLIP subject and CSD style embeddings",
                    )
                    processed += 1
            except Exception as exc:
                for artwork_id, _ in work_items:
                    log_event(
                        conn,
                        run_id,
                        level="error",
                        event_type="embedding_failed",
                        artwork_id=artwork_id,
                        message=str(exc),
                    )
                    errors += 1
    finally:
        finish_run(
            conn,
            run_id,
            images_processed=processed,
            images_skipped=skipped,
            errors_count=errors,
        )

    return {"processed": processed, "skipped": skipped, "errors": errors}


def _current_embedding_count(conn: sqlite3.Connection, config: AppConfig) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
          FROM artworks AS a
          JOIN embeddings AS e ON e.artwork_id = a.artwork_id
         WHERE a.validated = 1
           AND a.processed_path IS NOT NULL
           AND e.model_name_clip = ?
           AND e.model_version_clip = ?
           AND e.model_name_csd = ?
           AND e.model_version_csd = ?
        """,
        (
            config.models.clip_model_name,
            config.models.clip_model_version,
            config.models.csd_model_name,
            config.models.csd_model_version,
        ),
    ).fetchone()
    return int(row[0])


def _pending_artwork_batches(
    conn: sqlite3.Connection,
    config: AppConfig,
) -> Iterator[list[sqlite3.Row]]:
    """Keyset-stream only rows that need the configured model bundle."""

    last_artwork_id = ""
    while True:
        rows = conn.execute(
            """
            SELECT a.artwork_id, a.processed_path
              FROM artworks AS a
              LEFT JOIN embeddings AS e ON e.artwork_id = a.artwork_id
             WHERE a.validated = 1
               AND a.processed_path IS NOT NULL
               AND a.artwork_id > ?
               AND (
                   e.artwork_id IS NULL
                   OR e.model_name_clip IS NOT ?
                   OR e.model_version_clip IS NOT ?
                   OR e.model_name_csd IS NOT ?
                   OR e.model_version_csd IS NOT ?
               )
             ORDER BY a.artwork_id
             LIMIT ?
            """,
            (
                last_artwork_id,
                config.models.clip_model_name,
                config.models.clip_model_version,
                config.models.csd_model_name,
                config.models.csd_model_version,
                config.embeddings.batch_size,
            ),
        ).fetchall()
        if not rows:
            return
        yield rows
        last_artwork_id = str(rows[-1]["artwork_id"])


def _path_from_db(config: AppConfig, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return config.root_dir / path
