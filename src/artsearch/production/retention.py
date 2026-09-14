from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3

from artsearch.ingest.config import AppConfig
from artsearch.ingest.db import connect, init_db
from artsearch.production.config import LocalRetentionConfig


@dataclass(frozen=True)
class LocalRetentionResult:
    applied: bool
    eligible_artworks: int
    files_selected: int
    files_deleted: int
    bytes_selected: int
    bytes_deleted: int
    missing_files: int
    skipped_without_source: int
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["errors"] = list(self.errors)
        return payload


def apply_local_image_retention(
    database_path: str | Path,
    app_config: AppConfig,
    policy: LocalRetentionConfig,
    *,
    candidate_ids: Collection[str],
    apply: bool = False,
) -> LocalRetentionResult:
    """Delete bounded working files only after current embeddings and source evidence exist."""

    requested = sorted({value.strip() for value in candidate_ids if value.strip()})
    if not requested or not policy.enabled:
        return LocalRetentionResult(False, 0, 0, 0, 0, 0, 0, 0)

    with connect(database_path) as connection:
        init_db(connection)
        rows = _eligible_rows(connection, app_config, requested)

    selected: dict[Path, int] = {}
    skipped_without_source = 0
    missing_files = 0
    errors: list[str] = []
    for row in rows:
        if policy.require_source_reference and not _has_source_reference(row["candidate_json"]):
            skipped_without_source += 1
            continue
        paths = []
        if policy.delete_raw:
            paths.append((row["raw_path"], app_config.raw_dir))
        if policy.delete_processed:
            paths.append((row["processed_path"], app_config.processed_dir))
        for value, allowed_root in paths:
            if value is None:
                continue
            try:
                path = _safe_local_path(app_config.root_dir, str(value), allowed_root)
                if path in selected:
                    continue
                if not path.is_file():
                    missing_files += 1
                    continue
                selected[path] = path.stat().st_size
            except OSError as exc:
                _append_error(errors, f"{row['artwork_id']}: {type(exc).__name__}: {exc}")

    deleted = 0
    bytes_deleted = 0
    if apply:
        for path, byte_size in selected.items():
            try:
                path.unlink()
                deleted += 1
                bytes_deleted += byte_size
            except OSError as exc:
                _append_error(errors, f"{path.name}: {type(exc).__name__}: {exc}")

    return LocalRetentionResult(
        applied=apply,
        eligible_artworks=len(rows),
        files_selected=len(selected),
        files_deleted=deleted,
        bytes_selected=sum(selected.values()),
        bytes_deleted=bytes_deleted,
        missing_files=missing_files,
        skipped_without_source=skipped_without_source,
        errors=tuple(errors),
    )


def _eligible_rows(
    connection: sqlite3.Connection,
    app_config: AppConfig,
    candidate_ids: list[str],
) -> list[sqlite3.Row]:
    rows: dict[str, sqlite3.Row] = {}
    for offset in range(0, len(candidate_ids), 500):
        batch = candidate_ids[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        query = f"""
            SELECT DISTINCT a.artwork_id, a.raw_path, a.processed_path,
                   (
                       SELECT d.candidate_json
                         FROM artwork_filter_decisions AS d
                        WHERE d.candidate_id = a.source_id
                           OR d.candidate_id IN (
                               SELECT r.candidate_id
                                 FROM artwork_filter_routes AS r
                                WHERE r.artwork_id = a.artwork_id
                           )
                        ORDER BY d.processed_at DESC, d.decision_key DESC
                        LIMIT 1
                   ) AS candidate_json
              FROM artworks AS a
              JOIN embeddings AS e ON e.artwork_id = a.artwork_id
             WHERE a.validated = 1
               AND a.source_platform = 'bluesky'
               AND EXISTS (
                   SELECT 1
                     FROM artwork_filter_decisions AS accepted
                     LEFT JOIN artwork_filter_routes AS accepted_route
                       ON accepted_route.decision_key = accepted.decision_key
                    WHERE accepted.candidate_id IN ({placeholders})
                      AND accepted.decision = 'accept'
                      AND accepted.accepted_for_main_corpus = 1
                      AND (
                          a.source_id = accepted.candidate_id
                          OR accepted_route.artwork_id = a.artwork_id
                      )
               )
               AND e.model_name_clip = ?
               AND e.model_version_clip = ?
               AND e.model_name_csd = ?
               AND e.model_version_csd = ?
               AND e.clip_vector IS NOT NULL
               AND e.csd_style IS NOT NULL
               AND e.clip_dim > 0
               AND e.csd_style_dim > 0
               AND length(e.clip_vector) = e.clip_dim * 4
               AND length(e.csd_style) = e.csd_style_dim * 4
             ORDER BY a.artwork_id
        """
        parameters = (
            *batch,
            app_config.models.clip_model_name,
            app_config.models.clip_model_version,
            app_config.models.csd_model_name,
            app_config.models.csd_model_version,
        )
        for row in connection.execute(query, parameters):
            rows[str(row["artwork_id"])] = row
    return list(rows.values())


def _has_source_reference(candidate_json: object) -> bool:
    try:
        candidate = json.loads(str(candidate_json or ""))
    except json.JSONDecodeError:
        return False
    if not isinstance(candidate, dict):
        return False
    return any(
        isinstance(candidate.get(field), str) and bool(candidate[field].strip())
        for field in ("blob_cid", "fullsize_url", "thumbnail_url")
    )


def _safe_local_path(project_root: Path, value: str, allowed_root: Path) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else project_root / path).resolve()
    root = allowed_root.resolve()
    if not resolved.is_relative_to(root):
        raise OSError(f"retention path escapes {root}")
    return resolved


def _append_error(errors: list[str], message: str, *, limit: int = 100) -> None:
    if len(errors) < limit:
        errors.append(message)
