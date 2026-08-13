from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from collections.abc import Collection

from artsearch.ingest.db import connect, init_db
from artsearch.production.object_store import (
    ObjectRef,
    ObjectStore,
    content_addressed_key,
    file_identity,
)


@dataclass(frozen=True)
class CorpusPublishResult:
    eligible: int = 0
    published: int = 0
    unchanged: int = 0
    missing: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["errors"] = list(self.errors)
        return payload


def publish_corpus_originals(
    database_path: str | Path,
    project_root: str | Path,
    store: ObjectStore,
    *,
    namespace: str = "corpus/originals",
    candidate_ids: Collection[str] | None = None,
) -> CorpusPublishResult:
    """Publish accepted originals and checkpoint immutable object references in SQLite."""

    root = Path(project_root).resolve()
    counters = {
        "eligible": 0,
        "published": 0,
        "unchanged": 0,
        "missing": 0,
        "failed": 0,
    }
    errors: list[str] = []
    with connect(database_path) as connection:
        init_db(connection)
        rows = _eligible_rows(connection, candidate_ids)
        counters["eligible"] = len(rows)
        for row in rows:
            artwork_id = str(row["artwork_id"])
            expected_hash = str(row["file_hash"])
            if row["published_sha256"] == expected_hash and row["published_object_key"]:
                try:
                    if store.exists(str(row["published_object_key"])):
                        counters["unchanged"] += 1
                        continue
                except RuntimeError as exc:
                    _record_error(errors, f"{artwork_id}: checkpoint verification failed: {exc}")
            source = _resolve_local_path(root, str(row["raw_path"]))
            if not source.is_file():
                counters["missing"] += 1
                _record_error(errors, f"{artwork_id}: local accepted original is missing")
                continue
            try:
                actual_hash, _ = file_identity(source)
                if actual_hash != expected_hash:
                    raise ValueError("local original hash differs from SQLite")
                key = content_addressed_key(namespace, expected_hash, suffix=source.suffix.lower())
                ref = store.put_file(source, key, expected_sha256=expected_hash)
                _upsert_object_ref(connection, artwork_id, ref)
                connection.commit()
                counters["published"] += 1
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
                connection.rollback()
                counters["failed"] += 1
                detail = str(exc).replace(str(root), "<project>")
                _record_error(errors, f"{artwork_id}: {type(exc).__name__}: {detail}")
    return CorpusPublishResult(**counters, errors=tuple(errors))


def candidate_ids_from_jsonl(
    path: str | Path,
    *,
    required_decision: str | None = None,
) -> set[str]:
    candidate_ids: set[str] = set()
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid candidate JSON on line {line_number}: {exc}") from exc
            candidate_id = payload.get("candidate_id") if isinstance(payload, dict) else None
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError(f"candidate JSON line {line_number} has no candidate_id")
            if required_decision is not None and payload.get("decision") != required_decision:
                continue
            candidate_ids.add(candidate_id)
    return candidate_ids


def _eligible_rows(
    connection: sqlite3.Connection,
    candidate_ids: Collection[str] | None,
) -> list[sqlite3.Row]:
    query = """
            SELECT a.artwork_id, a.raw_path, a.file_hash,
                   o.content_sha256 AS published_sha256,
                   o.object_key AS published_object_key
              FROM artworks AS a
              LEFT JOIN artwork_objects AS o
                ON o.artwork_id = a.artwork_id AND o.role = 'original'
             WHERE a.validated = 1
               AND a.source_platform = 'bluesky'
               AND a.file_hash IS NOT NULL
    """
    if candidate_ids is None:
        return connection.execute(f"{query} ORDER BY a.artwork_id").fetchall()
    unique_ids = sorted(set(candidate_ids))
    rows: list[sqlite3.Row] = []
    for offset in range(0, len(unique_ids), 500):
        batch = unique_ids[offset : offset + 500]
        requested = ", ".join("(?)" for _ in batch)
        rows.extend(
            connection.execute(
                f"""
                WITH requested(candidate_id) AS (VALUES {requested})
                {query}
                   AND (
                       a.source_id IN (SELECT candidate_id FROM requested)
                       OR EXISTS (
                           SELECT 1
                             FROM artwork_filter_routes AS r
                             JOIN requested AS q ON q.candidate_id = r.candidate_id
                            WHERE r.artwork_id = a.artwork_id
                       )
                   )
                 ORDER BY a.artwork_id
                """,
                batch,
            ).fetchall()
        )
    return rows


def _resolve_local_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _record_error(errors: list[str], message: str, *, limit: int = 100) -> None:
    if len(errors) < limit:
        errors.append(message)


def _upsert_object_ref(
    connection: sqlite3.Connection,
    artwork_id: str,
    ref: ObjectRef,
) -> None:
    connection.execute(
        """
        INSERT INTO artwork_objects (
            artwork_id, role, object_key, object_uri, content_sha256,
            byte_size, etag
        ) VALUES (?, 'original', ?, ?, ?, ?, ?)
        ON CONFLICT(artwork_id, role) DO UPDATE SET
            object_key = excluded.object_key,
            object_uri = excluded.object_uri,
            content_sha256 = excluded.content_sha256,
            byte_size = excluded.byte_size,
            etag = excluded.etag,
            published_at = CURRENT_TIMESTAMP,
            verified_at = CURRENT_TIMESTAMP
        """,
        (
            artwork_id,
            ref.key,
            ref.uri,
            ref.sha256,
            ref.size,
            ref.etag,
        ),
    )
