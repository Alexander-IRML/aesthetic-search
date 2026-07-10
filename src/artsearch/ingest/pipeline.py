from __future__ import annotations

import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path

from artsearch.ingest.artists import ArtistRecord, load_artist_manifest, register_artists
from artsearch.ingest.config import AppConfig, load_config
from artsearch.ingest.db import (
    connect,
    find_artwork_by_raw_path,
    find_validated_by_hash,
    finish_run,
    init_db,
    insert_artwork,
    log_event,
    start_run,
    update_artwork_standardization,
)
from artsearch.ingest.hashing import perceptual_hash, phash_distance, sha256_file
from artsearch.ingest.standardize import standardize_image
from artsearch.ingest.validate import audit_image_file, supported_image


def initialize_database(config_path: str | Path = "config/config.yaml") -> Path:
    config = load_config(config_path)
    with connect(config.database_path) as conn:
        init_db(conn)
    return config.database_path


def register_artists_from_manifest(
    config_path: str | Path = "config/config.yaml",
    artists_path: str | Path = "config/artists.yaml",
) -> int:
    config = load_config(config_path)
    artists = load_artist_manifest(artists_path)
    with connect(config.database_path) as conn:
        init_db(conn)
        return register_artists(conn, artists)


def standardize_corpus(
    config_path: str | Path = "config/config.yaml",
    artists_path: str | Path = "config/artists.yaml",
) -> dict[str, int]:
    config = load_config(config_path)
    artists = load_artist_manifest(artists_path)
    config.processed_dir.mkdir(parents=True, exist_ok=True)

    processed = skipped = errors = 0
    with connect(config.database_path) as conn:
        init_db(conn)
        register_artists(conn, artists)
        run_id = start_run(conn, "standardize_corpus")
        try:
            for artist in artists:
                artist_raw_dir = config.raw_dir / artist.folder_name
                if not artist_raw_dir.exists():
                    log_event(
                        conn,
                        run_id,
                        level="warning",
                        event_type="missing_artist_folder",
                        message=f"Raw folder does not exist for artist {artist.artist_id}",
                        raw_path=str(artist_raw_dir),
                    )
                    continue

                for raw_path in sorted(artist_raw_dir.rglob("*")):
                    if not raw_path.is_file() or not supported_image(raw_path):
                        continue
                    try:
                        result = _process_one(conn, run_id, config, artist.artist_id, raw_path)
                    except Exception as exc:
                        log_event(
                            conn,
                            run_id,
                            level="error",
                            event_type="standardization_failed",
                            raw_path=_path_for_db(config, raw_path),
                            message=str(exc),
                        )
                        result = {"processed": 0, "skipped": 0, "errors": 1}
                    processed += result["processed"]
                    skipped += result["skipped"]
                    errors += result["errors"]
            errors += _audit_corpus_metadata(conn, run_id, config, artists)
        finally:
            finish_run(
                conn,
                run_id,
                images_processed=processed,
                images_skipped=skipped,
                errors_count=errors,
            )

    return {"processed": processed, "skipped": skipped, "errors": errors}


def _process_one(
    conn: sqlite3.Connection,
    run_id: int,
    config: AppConfig,
    artist_id: str,
    raw_path: Path,
) -> dict[str, int]:
    raw_rel = _path_for_db(config, raw_path)
    existing = find_artwork_by_raw_path(conn, raw_rel)

    audit = audit_image_file(raw_path)
    if not audit.ok:
        log_event(
            conn,
            run_id,
            level="error",
            event_type="audit_failed",
            raw_path=raw_rel,
            message=audit.error or "image audit failed",
        )
        return {"processed": 0, "skipped": 0, "errors": 1}

    file_hash = sha256_file(raw_path)
    existing_artwork_id = str(existing["artwork_id"]) if existing else None
    if (
        existing
        and existing["validated"]
        and existing["file_hash"] == file_hash
        and _processed_output_exists(config, existing["processed_path"])
    ):
        log_event(
            conn,
            run_id,
            level="info",
            event_type="already_validated",
            raw_path=raw_rel,
            artwork_id=existing_artwork_id,
            message="Artwork already has a validated row for the current file hash",
        )
        return {"processed": 0, "skipped": 1, "errors": 0}

    duplicate = find_validated_by_hash(
        conn,
        file_hash,
        exclude_artwork_id=existing_artwork_id,
    )
    if duplicate:
        log_event(
            conn,
            run_id,
            level="info",
            event_type="exact_duplicate_skipped",
            raw_path=raw_rel,
            artwork_id=duplicate["artwork_id"],
            message=f"Exact duplicate of validated artwork {duplicate['artwork_id']}",
        )
        return {"processed": 0, "skipped": 1, "errors": 0}

    artwork_id = existing_artwork_id or f"art_{uuid.uuid4().hex}"
    phash = perceptual_hash(raw_path)
    same_file_as_existing = bool(existing and existing["file_hash"] == file_hash)
    if same_file_as_existing:
        duplicate_of = existing["duplicate_of"]
        review_status = existing["review_status"] or "unreviewed"
        near_duplicate_of = None
    else:
        near_duplicate_of = _find_near_duplicate(
            conn,
            phash,
            config.duplicates.phash_distance_threshold,
            exclude_artwork_id=artwork_id,
        )
        duplicate_of = None
        review_status = "unreviewed"

    if not existing:
        insert_artwork(
            conn,
            {
                "artwork_id": artwork_id,
                "artist_id": artist_id,
                "raw_path": raw_rel,
                "source_platform": "manual",
                "file_hash": file_hash,
                "phash": phash,
                "review_status": review_status,
                "duplicate_of": duplicate_of,
            },
        )
    else:
        update_artwork_standardization(
            conn,
            artwork_id,
            {
                "artist_id": artist_id,
                "file_hash": file_hash,
                "phash": phash,
                "validated": 0,
                "review_status": review_status,
                "duplicate_of": duplicate_of,
            },
        )

    processed_path = _processed_path(config, raw_path)
    processed_db_path = _path_for_db(config, processed_path)
    standardized = standardize_image(raw_path, processed_path, config.images)
    transform = standardized.transform
    update_artwork_standardization(
        conn,
        artwork_id,
        {
            "processed_path": processed_db_path,
            "orig_width": standardized.orig_width,
            "orig_height": standardized.orig_height,
            "file_hash": file_hash,
            "phash": phash,
            "validated": 1,
            "review_status": review_status,
            "duplicate_of": duplicate_of,
            "scale_factor": transform.scale_factor,
            "pad_left": transform.pad_left,
            "pad_top": transform.pad_top,
            "pad_right": transform.pad_right,
            "pad_bottom": transform.pad_bottom,
            "crop_left": transform.crop_left,
            "crop_top": transform.crop_top,
            "crop_right": transform.crop_right,
            "crop_bottom": transform.crop_bottom,
        },
    )
    log_event(
        conn,
        run_id,
        level="info",
        event_type="standardized",
        raw_path=raw_rel,
        artwork_id=artwork_id,
        message=f"Standardized image with transform {asdict(transform)}",
    )
    if near_duplicate_of is not None:
        log_event(
            conn,
            run_id,
            level="warning",
            event_type="near_duplicate_review",
            raw_path=raw_rel,
            artwork_id=artwork_id,
            message=f"Likely near-duplicate of artwork {near_duplicate_of}",
        )
    return {"processed": 1, "skipped": 0, "errors": 0}


def _find_near_duplicate(
    conn: sqlite3.Connection,
    phash: str,
    distance_threshold: int,
    *,
    exclude_artwork_id: str | None = None,
) -> str | None:
    rows = conn.execute(
        "SELECT artwork_id, phash FROM artworks WHERE phash IS NOT NULL"
    ).fetchall()
    for row in rows:
        if exclude_artwork_id is not None and row["artwork_id"] == exclude_artwork_id:
            continue
        if phash_distance(phash, row["phash"]) <= distance_threshold:
            return str(row["artwork_id"])
    return None


def _processed_path(config: AppConfig, raw_path: Path) -> Path:
    raw_rel = raw_path.relative_to(config.raw_dir)
    return config.processed_dir / raw_rel.with_suffix(".jpg")


def _audit_corpus_metadata(
    conn: sqlite3.Connection,
    run_id: int,
    config: AppConfig,
    artists: list[ArtistRecord],
) -> int:
    errors = 0
    expected_raw_paths = set()
    for artist in artists:
        artist_raw_dir = config.raw_dir / artist.folder_name
        if not artist_raw_dir.exists():
            continue
        for raw_path in sorted(artist_raw_dir.rglob("*")):
            if raw_path.is_file() and supported_image(raw_path):
                expected_raw_paths.add(_path_for_db(config, raw_path))

    rows = conn.execute(
        "SELECT artwork_id, raw_path, processed_path, validated FROM artworks"
    ).fetchall()
    artwork_raw_paths = {row["raw_path"] for row in rows}

    for row in rows:
        raw_path = _path_from_db(config, row["raw_path"])
        if not raw_path.exists():
            _invalidate_artwork(conn, row["artwork_id"])
            log_event(
                conn,
                run_id,
                level="error",
                event_type="missing_raw_file",
                raw_path=row["raw_path"],
                artwork_id=row["artwork_id"],
                message="Artwork row references a raw file that does not exist",
            )
            errors += 1

        if row["validated"] and not row["processed_path"]:
            _invalidate_artwork(conn, row["artwork_id"])
            log_event(
                conn,
                run_id,
                level="error",
                event_type="missing_processed_path",
                raw_path=row["raw_path"],
                artwork_id=row["artwork_id"],
                message="Validated artwork row has no processed_path",
            )
            errors += 1
        elif row["validated"] and not _path_from_db(config, row["processed_path"]).exists():
            _invalidate_artwork(conn, row["artwork_id"])
            log_event(
                conn,
                run_id,
                level="error",
                event_type="missing_processed_file",
                raw_path=row["raw_path"],
                artwork_id=row["artwork_id"],
                message="Validated artwork row references a processed file that does not exist",
            )
            errors += 1

    for raw_path in sorted(expected_raw_paths - artwork_raw_paths):
        if _has_terminal_raw_event(conn, raw_path):
            continue
        log_event(
            conn,
            run_id,
            level="error",
            event_type="missing_artwork_row",
            raw_path=raw_path,
            message="Raw image has no artwork row",
        )
        errors += 1

    return errors


def _processed_output_exists(config: AppConfig, processed_path: str | None) -> bool:
    return bool(processed_path and _path_from_db(config, processed_path).exists())


def _invalidate_artwork(conn: sqlite3.Connection, artwork_id: str) -> None:
    update_artwork_standardization(conn, artwork_id, {"validated": 0})


def _has_terminal_raw_event(conn: sqlite3.Connection, raw_path: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
              FROM run_events
             WHERE raw_path = ?
               AND event_type IN (
                   'audit_failed',
                   'exact_duplicate_skipped',
                   'standardization_failed'
               )
             LIMIT 1
            """,
            (raw_path,),
        ).fetchone()
    )


def _path_for_db(config: AppConfig, path: Path) -> str:
    try:
        return str(path.relative_to(config.root_dir))
    except ValueError:
        return str(path)


def _path_from_db(config: AppConfig, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return config.root_dir / path
