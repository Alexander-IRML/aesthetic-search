from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import uuid

import numpy as np
from sqlalchemy import Engine, Table, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import Connection

from artsearch.postgres.ids import uuid7
from artsearch.postgres.schema import (
    actor_intake_state,
    artists,
    artwork_objects,
    artworks,
    embeddings,
    filter_decisions,
    filter_routes,
    model_versions,
    roster_intake_state,
    run_events,
    runs,
    sqlite_imports,
    outbox_events,
)


IMPORT_CONTRACT_VERSION = "sqlite-cutover-v1"


class SQLiteMigrationError(RuntimeError):
    """Raised when a SQLite snapshot cannot be published without data loss."""


@dataclass(frozen=True)
class SQLiteMigrationReport:
    import_id: str
    import_key: str
    source_name: str
    source_sha256: str
    source_size: int
    schema_revision: str
    scope: dict[str, object]
    row_counts: dict[str, int]
    verified: bool
    skipped: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _TableSpec:
    source_name: str
    target: Table
    transform: Callable[[Mapping[str, Any]], dict[str, Any]]
    scoped_query: str | None = None
    batch_size: int = 500


def publish_sqlite_snapshot(
    sqlite_path: str | Path,
    engine: Engine,
    *,
    candidate_ids: Sequence[str] | None = None,
    allow_new_full_snapshot: bool = False,
    allow_existing_catalog: bool = False,
    qdrant_collection_name: str | None = None,
    model_bundle: Mapping[str, Any] | None = None,
) -> SQLiteMigrationReport:
    """Transactionally upsert and verify a consistent SQLite snapshot."""

    source_path = Path(sqlite_path)
    if not source_path.is_file():
        raise SQLiteMigrationError(f"SQLite source does not exist: {source_path}")
    normalized_scope = sorted({value.strip() for value in candidate_ids or () if value.strip()})
    if candidate_ids is not None and not normalized_scope:
        raise SQLiteMigrationError("candidate scope was supplied but contains no candidate IDs")

    with tempfile.TemporaryDirectory(prefix="artsearch-pg-cutover-") as temp_dir:
        snapshot_path = Path(temp_dir) / "snapshot.sqlite3"
        _backup_sqlite(source_path, snapshot_path)
        source_sha256, source_size = _file_identity(snapshot_path)
        scope = _scope_metadata(normalized_scope if candidate_ids is not None else None)
        import_key = _import_key(source_sha256, scope)
        import_id = uuid7()

        try:
            with sqlite3.connect(snapshot_path) as source:
                source.row_factory = sqlite3.Row
                _prepare_scope(source, normalized_scope if candidate_ids is not None else None)
                with engine.begin() as target:
                    revision = _schema_revision(target)
                    existing = (
                        target.execute(
                            select(sqlite_imports).where(sqlite_imports.c.import_key == import_key)
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if existing is not None and existing["status"] == "completed":
                        return _existing_report(existing)

                    _guard_cutover(
                        target,
                        scoped=candidate_ids is not None,
                        allow_new_full_snapshot=allow_new_full_snapshot,
                        allow_existing_catalog=allow_existing_catalog,
                    )
                    _start_import(
                        target,
                        import_id=import_id,
                        import_key=import_key,
                        source_name=source_path.name,
                        source_sha256=source_sha256,
                        source_size=source_size,
                        schema_revision=revision,
                        scope=scope,
                    )

                    row_counts: dict[str, int] = {}
                    for spec in _table_specs(scoped=candidate_ids is not None):
                        row_counts[spec.source_name] = _import_table(source, target, spec)
                    _restore_duplicate_links(source, target, scoped=candidate_ids is not None)
                    _hydrate_artwork_text(target)
                    _hydrate_object_uris(target)
                    if model_bundle:
                        _register_model_bundle(target, model_bundle)
                    if qdrant_collection_name:
                        row_counts["qdrant_outbox_events"] = _enqueue_qdrant_events(
                            source,
                            target,
                            scoped=candidate_ids is not None,
                            collection_name=qdrant_collection_name,
                            publication_key=import_key,
                        )
                    _reset_identity_sequences(target)

                    verification = {
                        "contract": IMPORT_CONTRACT_VERSION,
                        "source_rows_returned_by_upsert": True,
                        "transactional": True,
                        "row_count_total": sum(row_counts.values()),
                    }
                    target.execute(
                        update(sqlite_imports)
                        .where(sqlite_imports.c.import_key == import_key)
                        .values(
                            status="completed",
                            completed_at=func.now(),
                            row_counts=row_counts,
                            verification=verification,
                            error_message=None,
                        )
                    )
        except Exception as exc:
            _record_failed_import(
                engine,
                import_id=import_id,
                import_key=import_key,
                source_name=source_path.name,
                source_sha256=source_sha256,
                source_size=source_size,
                scope=scope,
                error=exc,
            )
            if isinstance(exc, SQLiteMigrationError):
                raise
            raise SQLiteMigrationError(
                f"SQLite-to-PostgreSQL publication failed: {type(exc).__name__}: {exc}"
            ) from exc

    return SQLiteMigrationReport(
        import_id=str(import_id),
        import_key=import_key,
        source_name=source_path.name,
        source_sha256=source_sha256,
        source_size=source_size,
        schema_revision=revision,
        scope=scope,
        row_counts=row_counts,
        verified=True,
        skipped=False,
    )


def candidate_ids_from_jsonl(path: str | Path) -> list[str]:
    candidate_ids: list[str] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SQLiteMigrationError(
                    f"invalid JSON on line {line_number} of {path}: {exc}"
                ) from exc
            candidate_id = str(payload.get("candidate_id", "")).strip()
            if not candidate_id:
                raise SQLiteMigrationError(
                    f"candidate_id is missing on line {line_number} of {path}"
                )
            candidate_ids.append(candidate_id)
    return sorted(set(candidate_ids))


def _backup_sqlite(source_path: Path, snapshot_path: Path) -> None:
    uri = f"file:{source_path.resolve()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as source, sqlite3.connect(snapshot_path) as target:
            source.backup(target)
    except sqlite3.Error as exc:
        raise SQLiteMigrationError(f"could not snapshot SQLite database: {exc}") from exc


def _file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _scope_metadata(candidate_ids: Sequence[str] | None) -> dict[str, object]:
    if candidate_ids is None:
        return {"mode": "full"}
    digest = hashlib.sha256("\n".join(candidate_ids).encode("utf-8")).hexdigest()
    return {"mode": "candidates", "candidate_count": len(candidate_ids), "sha256": digest}


def _import_key(source_sha256: str, scope: Mapping[str, object]) -> str:
    payload = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        f"{IMPORT_CONTRACT_VERSION}|{source_sha256}|{payload}".encode("utf-8")
    ).hexdigest()


def _prepare_scope(source: sqlite3.Connection, candidate_ids: Sequence[str] | None) -> None:
    if candidate_ids is None:
        return
    source.execute("CREATE TEMP TABLE migration_candidate_ids (candidate_id TEXT PRIMARY KEY)")
    source.executemany(
        "INSERT INTO migration_candidate_ids (candidate_id) VALUES (?)",
        ((candidate_id,) for candidate_id in candidate_ids),
    )


def _schema_revision(connection: Connection) -> str:
    try:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except Exception as exc:
        raise SQLiteMigrationError(
            "PostgreSQL schema is not migrated; run `artsearch-postgres upgrade` first"
        ) from exc
    if not revision:
        raise SQLiteMigrationError("PostgreSQL Alembic revision is empty")
    return str(revision)


def _guard_cutover(
    connection: Connection,
    *,
    scoped: bool,
    allow_new_full_snapshot: bool,
    allow_existing_catalog: bool,
) -> None:
    completed = int(
        connection.execute(
            select(func.count())
            .select_from(sqlite_imports)
            .where(sqlite_imports.c.status == "completed")
        ).scalar_one()
    )
    if not scoped and completed and not allow_new_full_snapshot:
        raise SQLiteMigrationError(
            "a different full snapshot was already published; use a candidate-scoped "
            "incremental publication or explicitly allow a replacement snapshot"
        )
    catalog_count = int(connection.execute(select(func.count()).select_from(artworks)).scalar_one())
    if completed == 0 and catalog_count and not allow_existing_catalog:
        raise SQLiteMigrationError(
            "target catalog is not empty and has no migration ledger; refusing an unverified merge"
        )


def _start_import(
    connection: Connection,
    *,
    import_id: uuid.UUID,
    import_key: str,
    source_name: str,
    source_sha256: str,
    source_size: int,
    schema_revision: str,
    scope: dict[str, object],
) -> None:
    statement = (
        postgresql_insert(sqlite_imports)
        .values(
            import_id=import_id,
            import_key=import_key,
            source_name=source_name,
            source_sha256=source_sha256,
            source_size=source_size,
            scope=scope,
            schema_revision=schema_revision,
            status="running",
        )
        .on_conflict_do_update(
            index_elements=[sqlite_imports.c.import_key],
            set_={
                "import_id": import_id,
                "status": "running",
                "started_at": func.now(),
                "completed_at": None,
                "row_counts": {},
                "verification": {},
                "error_message": None,
            },
        )
    )
    connection.execute(statement)


def _record_failed_import(
    engine: Engine,
    *,
    import_id: uuid.UUID,
    import_key: str,
    source_name: str,
    source_sha256: str,
    source_size: int,
    scope: dict[str, object],
    error: Exception,
) -> None:
    try:
        with engine.begin() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            if revision is None:
                return
            connection.execute(
                postgresql_insert(sqlite_imports)
                .values(
                    import_id=import_id,
                    import_key=import_key,
                    source_name=source_name,
                    source_sha256=source_sha256,
                    source_size=source_size,
                    scope=scope,
                    schema_revision=str(revision),
                    status="failed",
                    completed_at=func.now(),
                    error_message=f"{type(error).__name__}: {error}"[:4000],
                )
                .on_conflict_do_update(
                    index_elements=[sqlite_imports.c.import_key],
                    set_={
                        "status": "failed",
                        "completed_at": func.now(),
                        "error_message": f"{type(error).__name__}: {error}"[:4000],
                    },
                )
            )
    except Exception:
        # The original migration error carries the useful failure context.
        return


def _existing_report(row: Mapping[str, Any]) -> SQLiteMigrationReport:
    verification = dict(row["verification"] or {})
    return SQLiteMigrationReport(
        import_id=str(row["import_id"]),
        import_key=str(row["import_key"]),
        source_name=str(row["source_name"]),
        source_sha256=str(row["source_sha256"]),
        source_size=int(row["source_size"]),
        schema_revision=str(row["schema_revision"]),
        scope=dict(row["scope"] or {}),
        row_counts={key: int(value) for key, value in dict(row["row_counts"] or {}).items()},
        verified=bool(verification.get("source_rows_returned_by_upsert")),
        skipped=True,
    )


def _table_specs(*, scoped: bool) -> tuple[_TableSpec, ...]:
    artwork_scope = """
        SELECT a.* FROM artworks a
        JOIN migration_candidate_ids scope ON scope.candidate_id = a.source_id
    """
    specs = (
        _TableSpec(
            "artists",
            artists,
            _artist_row,
            """
            SELECT DISTINCT ar.* FROM artists ar
            JOIN artworks a ON a.artist_id = ar.artist_id
            JOIN migration_candidate_ids scope ON scope.candidate_id = a.source_id
            """,
        ),
        _TableSpec("artworks", artworks, _artwork_row, artwork_scope),
        _TableSpec(
            "artwork_objects",
            artwork_objects,
            _object_row,
            """
            SELECT o.* FROM artwork_objects o
            JOIN artworks a ON a.artwork_id = o.artwork_id
            JOIN migration_candidate_ids scope ON scope.candidate_id = a.source_id
            """,
        ),
        _TableSpec(
            "embeddings",
            embeddings,
            _embedding_row,
            """
            SELECT e.* FROM embeddings e
            JOIN artworks a ON a.artwork_id = e.artwork_id
            JOIN migration_candidate_ids scope ON scope.candidate_id = a.source_id
            """,
            batch_size=100,
        ),
        _TableSpec("runs", runs, _run_row, None if scoped else "SELECT * FROM runs"),
        _TableSpec(
            "run_events",
            run_events,
            _run_event_row,
            None if scoped else "SELECT * FROM run_events",
        ),
        _TableSpec(
            "artwork_filter_decisions",
            filter_decisions,
            _decision_row,
            """
            SELECT d.* FROM artwork_filter_decisions d
            JOIN migration_candidate_ids scope ON scope.candidate_id = d.candidate_id
            """,
        ),
        _TableSpec(
            "artwork_filter_routes",
            filter_routes,
            _route_row,
            """
            SELECT r.* FROM artwork_filter_routes r
            JOIN migration_candidate_ids scope ON scope.candidate_id = r.candidate_id
            """,
        ),
        _TableSpec(
            "bluesky_actor_intake_state",
            actor_intake_state,
            _actor_state_row,
            "SELECT * FROM bluesky_actor_intake_state",
        ),
        _TableSpec(
            "bluesky_roster_intake_state",
            roster_intake_state,
            _roster_state_row,
            "SELECT * FROM bluesky_roster_intake_state",
        ),
    )
    if not scoped:
        return tuple(
            _TableSpec(
                source_name=spec.source_name,
                target=spec.target,
                transform=spec.transform,
                batch_size=spec.batch_size,
            )
            for spec in specs
        )
    return tuple(spec for spec in specs if spec.scoped_query is not None)


def _import_table(
    source: sqlite3.Connection,
    target: Connection,
    spec: _TableSpec,
) -> int:
    if not _sqlite_table_exists(source, spec.source_name):
        return 0
    query = spec.scoped_query or f"SELECT * FROM {spec.source_name}"
    cursor = source.execute(query)
    count = 0
    while rows := cursor.fetchmany(spec.batch_size):
        values = [spec.transform(dict(row)) for row in rows]
        _upsert_verified(target, spec.target, values)
        count += len(values)
    return count


def _upsert_verified(connection: Connection, table: Table, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    primary_keys = list(table.primary_key.columns)
    statement = postgresql_insert(table).values(rows)
    updates = {
        column.name: statement.excluded[column.name]
        for column in table.columns
        if column not in primary_keys and column.name in rows[0] and column.computed is None
    }
    statement = statement.on_conflict_do_update(
        index_elements=primary_keys,
        set_=updates,
    ).returning(*primary_keys)
    returned = connection.execute(statement).fetchall()
    if len(returned) != len(rows):
        raise SQLiteMigrationError(
            f"verification failed for {table.fullname}: source={len(rows)}, returned={len(returned)}"
        )


def _restore_duplicate_links(
    source: sqlite3.Connection,
    target: Connection,
    *,
    scoped: bool,
) -> None:
    if not _sqlite_table_exists(source, "artworks"):
        return
    query = "SELECT artwork_id, duplicate_of FROM artworks WHERE duplicate_of IS NOT NULL"
    if scoped:
        query = """
            SELECT a.artwork_id, a.duplicate_of FROM artworks a
            JOIN migration_candidate_ids scope ON scope.candidate_id = a.source_id
            WHERE a.duplicate_of IS NOT NULL
        """
    for row in source.execute(query):
        target.execute(
            update(artworks)
            .where(artworks.c.artwork_id == row["artwork_id"])
            .values(duplicate_of=row["duplicate_of"])
        )


def _hydrate_artwork_text(connection: Connection) -> None:
    connection.execute(
        text(
            """
            WITH latest AS (
              SELECT DISTINCT ON (candidate_id)
                     candidate_id, candidate
                FROM ingest.artwork_filter_decisions
               ORDER BY candidate_id, processed_at DESC, decision_key DESC
            )
            UPDATE catalog.artworks a
               SET post_text = NULLIF(latest.candidate ->> 'post_text', ''),
                   alt_text = NULLIF(latest.candidate ->> 'alt_text', ''),
                   metadata = a.metadata || jsonb_strip_nulls(jsonb_build_object(
                     'post_uri', latest.candidate ->> 'post_uri',
                     'author_did', latest.candidate ->> 'author_did',
                     'author_handle', latest.candidate ->> 'author_handle',
                     'created_at', latest.candidate ->> 'created_at'
                   )),
                   updated_at = now()
              FROM latest
             WHERE a.source_platform = 'bluesky'
               AND a.source_id = latest.candidate_id
            """
        )
    )


def _hydrate_object_uris(connection: Connection) -> None:
    connection.execute(
        text(
            """
            UPDATE catalog.artworks a
               SET object_uri = o.object_uri,
                   updated_at = now()
              FROM catalog.artwork_objects o
             WHERE o.artwork_id = a.artwork_id
               AND o.role = 'original'
               AND a.object_uri IS DISTINCT FROM o.object_uri
            """
        )
    )


def _reset_identity_sequences(connection: Connection) -> None:
    for qualified_table, column in (
        ("ingest.runs", "run_id"),
        ("ingest.run_events", "event_id"),
    ):
        connection.execute(
            text(
                f"""
                SELECT setval(
                  pg_get_serial_sequence('{qualified_table}', '{column}'),
                  COALESCE((SELECT MAX({column}) FROM {qualified_table}), 1),
                  EXISTS (SELECT 1 FROM {qualified_table})
                )
                """
            )
        )


def _register_model_bundle(
    connection: Connection,
    bundle: Mapping[str, Any],
) -> None:
    required = {
        "model_bundle_version",
        "clip_model_name",
        "clip_model_revision",
        "csd_model_name",
        "csd_model_revision",
        "clip_dimension",
        "csd_dimension",
    }
    missing = required - set(bundle)
    if missing:
        raise SQLiteMigrationError(f"model bundle is missing fields: {sorted(missing)}")
    values = {column: bundle[column] for column in required}
    values["status"] = bundle.get("status", "active")
    values["metadata"] = dict(bundle.get("metadata", {}))
    statement = postgresql_insert(model_versions).values(**values)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[model_versions.c.model_bundle_version],
            set_={
                "status": statement.excluded.status,
                "metadata": statement.excluded.metadata,
            },
        )
    )
    connection.execute(
        update(embeddings)
        .where(
            embeddings.c.model_name_clip == values["clip_model_name"],
            embeddings.c.model_version_clip == values["clip_model_revision"],
            embeddings.c.model_name_csd == values["csd_model_name"],
            embeddings.c.model_version_csd == values["csd_model_revision"],
            embeddings.c.clip_dim == values["clip_dimension"],
            embeddings.c.csd_style_dim == values["csd_dimension"],
        )
        .values(model_bundle_version=values["model_bundle_version"])
    )


def _enqueue_qdrant_events(
    source: sqlite3.Connection,
    target: Connection,
    *,
    scoped: bool,
    collection_name: str,
    publication_key: str,
) -> int:
    query = "SELECT artwork_id FROM artworks ORDER BY artwork_id"
    if scoped:
        query = """
            SELECT a.artwork_id FROM artworks a
            JOIN migration_candidate_ids scope ON scope.candidate_id = a.source_id
            ORDER BY a.artwork_id
        """
    cursor = source.execute(query)
    inserted = 0
    while rows := cursor.fetchmany(500):
        values = [
            {
                "event_id": uuid7(),
                "aggregate_type": "artwork",
                "aggregate_id": str(row["artwork_id"]),
                "event_type": "qdrant.reconcile_artwork",
                "payload": {
                    "artwork_id": str(row["artwork_id"]),
                    "collection_name": collection_name,
                },
                "idempotency_key": (
                    f"qdrant:{collection_name}:{row['artwork_id']}:{publication_key}"
                ),
            }
            for row in rows
        ]
        returned = target.execute(
            postgresql_insert(outbox_events)
            .values(values)
            .on_conflict_do_nothing(index_elements=[outbox_events.c.idempotency_key])
            .returning(outbox_events.c.event_id)
        ).fetchall()
        inserted += len(returned)
    return inserted


def _sqlite_table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _artist_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artist_id": row["artist_id"],
        "display_name": row["display_name"],
        "folder_name": row["folder_name"],
        "source_platform": row.get("source_platform") or "manual",
        "source_url": row.get("source_url"),
        "notes": row.get("notes"),
        "date_added": _datetime(row.get("date_added")),
    }


def _artwork_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artwork_id": row["artwork_id"],
        "artist_id": row["artist_id"],
        "raw_path": row["raw_path"],
        "processed_path": row.get("processed_path"),
        "source_platform": row.get("source_platform") or "manual",
        "source_id": row.get("source_id"),
        "orig_width": row.get("orig_width"),
        "orig_height": row.get("orig_height"),
        "file_hash": row.get("file_hash"),
        "phash": row.get("phash"),
        "is_sfw": _boolean(row.get("is_sfw")),
        "demo_eligible": bool(row.get("demo_eligible", 0)),
        "validated": bool(row.get("validated", 0)),
        "review_status": row.get("review_status") or "unreviewed",
        "duplicate_of": None,
        "scale_factor": row.get("scale_factor"),
        "pad_left": row.get("pad_left") or 0,
        "pad_top": row.get("pad_top") or 0,
        "pad_right": row.get("pad_right") or 0,
        "pad_bottom": row.get("pad_bottom") or 0,
        "crop_left": row.get("crop_left") or 0,
        "crop_top": row.get("crop_top") or 0,
        "crop_right": row.get("crop_right") or 0,
        "crop_bottom": row.get("crop_bottom") or 0,
        "date_added": _datetime(row.get("date_added")),
        "notes": row.get("notes"),
    }


def _object_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artwork_id": row["artwork_id"],
        "role": row["role"],
        "object_key": row["object_key"],
        "object_uri": row["object_uri"],
        "content_sha256": row["content_sha256"],
        "byte_size": row["byte_size"],
        "etag": row.get("etag"),
        "published_at": _datetime(row.get("published_at")),
        "verified_at": _datetime(row.get("verified_at")),
    }


def _embedding_row(row: Mapping[str, Any]) -> dict[str, Any]:
    clip_dim = _integer_or_none(row.get("clip_dim"))
    csd_dim = _integer_or_none(row.get("csd_style_dim"))
    return {
        "artwork_id": row["artwork_id"],
        "clip_vector": _vector(row.get("clip_vector"), clip_dim, expected=512, name="CLIP"),
        "clip_dim": clip_dim,
        "csd_style": _vector(row.get("csd_style"), csd_dim, expected=768, name="CSD"),
        "csd_style_dim": csd_dim,
        "model_name_clip": row.get("model_name_clip"),
        "model_version_clip": row.get("model_version_clip"),
        "model_name_csd": row.get("model_name_csd"),
        "model_version_csd": row.get("model_version_csd"),
        "date_computed": _datetime(row.get("date_computed")),
    }


def _run_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "script_name": row["script_name"],
        "started_at": _datetime(row.get("started_at")),
        "finished_at": _datetime(row.get("finished_at")),
        "images_processed": row.get("images_processed") or 0,
        "images_skipped": row.get("images_skipped") or 0,
        "errors_count": row.get("errors_count") or 0,
        "notes": row.get("notes"),
    }


def _run_event_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "run_id": row.get("run_id"),
        "level": row["level"],
        "event_type": row["event_type"],
        "raw_path": row.get("raw_path"),
        "artwork_id": row.get("artwork_id"),
        "message": row["message"],
        "created_at": _datetime(row.get("created_at")),
    }


def _decision_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "decision_key": row["decision_key"],
        "candidate_id": row["candidate_id"],
        "author_did": row.get("author_did"),
        "post_uri": row.get("post_uri"),
        "image_index": row.get("image_index"),
        "image_sha256": row.get("image_sha256"),
        "decision": row["decision"],
        "predicted_class": row["predicted_class"],
        "accepted_for_main_corpus": bool(row["accepted_for_main_corpus"]),
        "route": row["route"],
        "final_score": row["final_score"],
        "confidence": row["confidence"],
        "reason_codes": _json(row.get("reason_codes_json"), expected=list),
        "candidate": _json(row.get("candidate_json"), expected=dict),
        "evidence": _json(row.get("evidence_json"), expected=dict),
        "model_id": row["model_id"],
        "model_revision": row.get("model_revision"),
        "config_version": row["config_version"],
        "config_hash": row["config_hash"],
        "prompt_version": row.get("prompt_version"),
        "classifier_version": row.get("classifier_version"),
        "software_version": row["software_version"],
        "processed_at": _datetime(row.get("processed_at")),
        "duration_ms": row["duration_ms"],
        "error_type": row.get("error_type"),
        "error_message": row.get("error_message"),
        "date_added": _datetime(row.get("date_added")),
    }


def _route_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "route_key": row["route_key"],
        "decision_key": row["decision_key"],
        "candidate_id": row["candidate_id"],
        "target": row["target"],
        "status": row["status"],
        "local_path": row.get("local_path"),
        "image_sha256": row.get("image_sha256"),
        "perceptual_hash": row.get("perceptual_hash"),
        "width": row.get("width"),
        "height": row.get("height"),
        "artwork_id": row.get("artwork_id"),
        "error_type": row.get("error_type"),
        "error_message": row.get("error_message"),
        "routed_at": _datetime(row.get("routed_at")),
    }


def _actor_state_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "actor_key": row["actor_key"],
        "actor": row["actor"],
        "high_water_post_uri": row.get("high_water_post_uri"),
        "pending_high_water_post_uri": row.get("pending_high_water_post_uri"),
        "resume_cursor": row.get("resume_cursor"),
        "last_scan_complete": bool(row.get("last_scan_complete", 0)),
        "last_candidate_count": row.get("last_candidate_count") or 0,
        "last_error": row.get("last_error"),
        "last_attempt_at": _datetime(row.get("last_attempt_at")),
        "last_completed_at": _datetime(row.get("last_completed_at")),
    }


def _roster_state_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "roster_key": row["roster_key"],
        "actor_count": row["actor_count"],
        "next_actor_index": row.get("next_actor_index") or 0,
        "updated_at": _datetime(row.get("updated_at")),
    }


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _boolean(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _integer_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _json(value: Any, *, expected: type[list[Any]] | type[dict[str, Any]]) -> Any:
    parsed = json.loads(value) if isinstance(value, str) else value
    if parsed is None:
        parsed = expected()
    if not isinstance(parsed, expected):
        raise SQLiteMigrationError(
            f"expected {expected.__name__} JSON, got {type(parsed).__name__}"
        )
    return parsed


def _vector(
    blob: Any,
    dimension: int | None,
    *,
    expected: int,
    name: str,
) -> list[float] | None:
    if blob is None:
        return None
    if dimension != expected:
        raise SQLiteMigrationError(
            f"{name} embedding dimension {dimension!r} is incompatible with expected {expected}"
        )
    values = np.frombuffer(bytes(blob), dtype=np.float32)
    if values.shape != (expected,) or not np.isfinite(values).all():
        raise SQLiteMigrationError(f"{name} embedding is malformed or non-finite")
    if not math.isfinite(float(np.linalg.norm(values))):
        raise SQLiteMigrationError(f"{name} embedding norm is non-finite")
    return values.astype(np.float16).astype(np.float32).tolist()
