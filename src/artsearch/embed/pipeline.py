from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from artsearch.embed.models import EmbeddingProvider, HuggingFaceEmbeddingProvider
from artsearch.embed.storage import find_embedding, model_versions_match, upsert_embedding
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
    processed = skipped = errors = 0
    try:
        candidates = _validated_artworks(conn)
        for batch in _chunks(candidates, config.embeddings.batch_size):
            work_items = []
            for row in batch:
                embedding_row = find_embedding(conn, row["artwork_id"])
                if model_versions_match(embedding_row, config.models):
                    log_event(
                        conn,
                        run_id,
                        level="info",
                        event_type="embedding_already_current",
                        artwork_id=row["artwork_id"],
                        message="Embedding row already matches configured model versions",
                    )
                    skipped += 1
                    continue

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
                        message="Computed CLIP and DINO embeddings",
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


def _validated_artworks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT artwork_id, processed_path
          FROM artworks
         WHERE validated = 1
           AND processed_path IS NOT NULL
         ORDER BY artwork_id
        """
    ).fetchall()


def _chunks(rows: Sequence[sqlite3.Row], batch_size: int) -> list[Sequence[sqlite3.Row]]:
    return [rows[index : index + batch_size] for index in range(0, len(rows), batch_size)]


def _path_from_db(config: AppConfig, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return config.root_dir / path
