from __future__ import annotations

from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import random
import sqlite3
from time import perf_counter, sleep
from typing import Literal

from artsearch.ingest.db import connect, init_db
from artsearch.production.object_store import (
    ObjectCollisionError,
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
    bytes_uploaded: int = 0
    duration_ms: float = 0.0
    upload_concurrency: int = 1
    objects_per_second: float = 0.0
    mib_per_second: float = 0.0
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["errors"] = list(self.errors)
        return payload


@dataclass(frozen=True)
class _PublishOutcome:
    artwork_id: str
    status: Literal["published", "unchanged", "missing", "failed"]
    ref: ObjectRef | None = None
    error: str | None = None


def publish_corpus_originals(
    database_path: str | Path,
    project_root: str | Path,
    store: ObjectStore,
    *,
    namespace: str = "corpus/originals",
    candidate_ids: Collection[str] | None = None,
    upload_concurrency: int = 1,
    checkpoint_batch_size: int = 256,
    max_attempts: int = 4,
    retry_base_seconds: float = 0.5,
) -> CorpusPublishResult:
    """Publish accepted originals and checkpoint immutable object references in SQLite."""

    if upload_concurrency <= 0 or checkpoint_batch_size <= 0 or max_attempts <= 0:
        raise ValueError("publication concurrency, checkpoint size, and attempts must be positive")
    if retry_base_seconds < 0:
        raise ValueError("publication retry delay must be non-negative")
    started = perf_counter()
    root = Path(project_root).resolve()
    counters = {
        "eligible": 0,
        "published": 0,
        "unchanged": 0,
        "missing": 0,
        "failed": 0,
    }
    bytes_uploaded = 0
    errors: list[str] = []
    with connect(database_path) as connection:
        init_db(connection)
        rows = _eligible_rows(connection, candidate_ids)
    counters["eligible"] = len(rows)
    worker_count = min(upload_concurrency, len(rows)) if rows else upload_concurrency
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="object-publish") as pool:
        for offset in range(0, len(rows), checkpoint_batch_size):
            batch = rows[offset : offset + checkpoint_batch_size]
            outcomes = list(
                pool.map(
                    lambda row: _publish_row(
                        row,
                        root=root,
                        store=store,
                        namespace=namespace,
                        max_attempts=max_attempts,
                        retry_base_seconds=retry_base_seconds,
                    ),
                    batch,
                )
            )
            publishable = [outcome for outcome in outcomes if outcome.ref is not None]
            if publishable:
                try:
                    with connect(database_path) as connection:
                        init_db(connection)
                        for outcome in publishable:
                            assert outcome.ref is not None
                            _upsert_object_ref(connection, outcome.artwork_id, outcome.ref)
                        connection.commit()
                except sqlite3.DatabaseError as exc:
                    detail = str(exc).replace(str(root), "<project>")
                    counters["failed"] += len(publishable)
                    for outcome in publishable:
                        _record_error(
                            errors,
                            f"{outcome.artwork_id}: {type(exc).__name__}: {detail}",
                        )
                    publishable = []

            checkpointed = {outcome.artwork_id for outcome in publishable}
            for outcome in outcomes:
                if outcome.status == "published":
                    if outcome.artwork_id not in checkpointed:
                        continue
                    counters["published"] += 1
                    if outcome.ref is not None and outcome.ref.created:
                        bytes_uploaded += outcome.ref.size
                elif outcome.status == "unchanged":
                    counters["unchanged"] += 1
                elif outcome.status == "missing":
                    counters["missing"] += 1
                else:
                    counters["failed"] += 1
                if outcome.error:
                    _record_error(errors, outcome.error)

    duration = max(perf_counter() - started, 1e-9)
    completed = counters["published"] + counters["unchanged"]
    return CorpusPublishResult(
        **counters,
        bytes_uploaded=bytes_uploaded,
        duration_ms=round(duration * 1000.0, 3),
        upload_concurrency=worker_count,
        objects_per_second=round(completed / duration, 3),
        mib_per_second=round(bytes_uploaded / (1024 * 1024) / duration, 3),
        errors=tuple(errors),
    )


def _publish_row(
    row: sqlite3.Row,
    *,
    root: Path,
    store: ObjectStore,
    namespace: str,
    max_attempts: int,
    retry_base_seconds: float,
) -> _PublishOutcome:
    artwork_id = str(row["artwork_id"])
    expected_hash = str(row["file_hash"])
    source = _resolve_local_path(root, str(row["raw_path"]))
    if not source.is_file():
        return _PublishOutcome(
            artwork_id,
            "missing",
            error=f"{artwork_id}: local accepted original is missing",
        )
    try:
        actual_hash, _ = file_identity(source)
        if actual_hash != expected_hash:
            raise ValueError("local original hash differs from SQLite")
        key = content_addressed_key(namespace, expected_hash, suffix=source.suffix.lower())
    except (OSError, RuntimeError, ValueError) as exc:
        return _failed_outcome(artwork_id, exc, root)

    for attempt in range(max_attempts):
        try:
            if row["published_sha256"] == expected_hash and row["published_object_key"]:
                if store.exists(str(row["published_object_key"])):
                    return _PublishOutcome(artwork_id, "unchanged")
            ref = store.put_file(source, key, expected_sha256=expected_hash)
            return _PublishOutcome(artwork_id, "published", ref=ref)
        except ObjectCollisionError as exc:
            return _failed_outcome(artwork_id, exc, root)
        except (OSError, RuntimeError) as exc:
            if attempt + 1 == max_attempts:
                return _failed_outcome(artwork_id, exc, root)
            delay = retry_base_seconds * (2**attempt) * (0.75 + random.random() * 0.5)
            sleep(delay)
    raise AssertionError("publication retry loop did not return")


def _failed_outcome(artwork_id: str, exc: Exception, root: Path) -> _PublishOutcome:
    detail = str(exc).replace(str(root), "<project>")
    return _PublishOutcome(
        artwork_id,
        "failed",
        error=f"{artwork_id}: {type(exc).__name__}: {detail}",
    )


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
