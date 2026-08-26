from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping, Sequence
import uuid

import numpy as np
from sqlalchemy import ARRAY, Engine, Text, bindparam, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from artsearch.ingest.config import AppConfig
from artsearch.postgres.ids import uuid7
from artsearch.postgres.repositories import OutboxEvent, OutboxRepository
from artsearch.postgres.schema import artworks, outbox_events, vector_index_points
from artsearch.production.config import QdrantConfig
from artsearch.retrieval.qdrant import (
    QdrantIntegrationError,
    ensure_qdrant_collection,
    model_bundle_version,
    point_id_for_artwork,
    promote_qdrant_alias,
)


EVENT_TYPE = "qdrant.reconcile_artwork"


@dataclass(frozen=True)
class QdrantOutboxResult:
    collection_name: str
    claimed: int
    upserted: int
    deleted: int
    unchanged: int
    remote_count: int
    collection_created: bool
    alias_promoted: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def drain_qdrant_outbox(
    engine: Engine,
    app_config: AppConfig,
    config: QdrantConfig,
    client: Any,
    *,
    worker_id: str,
    batch_size: int | None = None,
    max_events: int | None = None,
) -> QdrantOutboxResult:
    """Publish canonical PostgreSQL changes to Qdrant with idempotent retries."""

    if max_events is not None and max_events <= 0:
        raise ValueError("max_events must be positive")
    claim_size = batch_size or config.batch_size
    if claim_size <= 0:
        raise ValueError("batch_size must be positive")

    ensured = ensure_qdrant_collection(client, config, app_config.models)
    try:
        initial_remote_count = int(client.count(config.collection_name, exact=True).count)
    except Exception as exc:
        raise QdrantIntegrationError(f"could not count Qdrant before outbox drain: {exc}") from exc
    with engine.connect() as connection:
        initial_state_count = int(
            connection.execute(
                select(func.count())
                .select_from(vector_index_points)
                .where(vector_index_points.c.collection_name == config.collection_name)
            ).scalar_one()
        )
    if ensured.created or initial_remote_count != initial_state_count:
        with engine.begin() as connection:
            connection.execute(
                delete(vector_index_points).where(
                    vector_index_points.c.collection_name == config.collection_name
                )
            )
        queue_full_qdrant_reconciliation(
            engine,
            config.collection_name,
            reason="collection-created" if ensured.created else "remote-count-drift",
        )

    repository = OutboxRepository(engine)
    repository.release_stale()
    claimed = upserted = deleted_count = unchanged = 0
    while max_events is None or claimed < max_events:
        remaining = claim_size if max_events is None else min(claim_size, max_events - claimed)
        events = repository.claim(worker_id, limit=remaining, event_type=EVENT_TYPE)
        if not events:
            break
        claimed += len(events)
        try:
            counts = _reconcile_claim(engine, app_config, config, client, worker_id, events)
        except Exception as exc:
            for event in events:
                repository.mark_failed(event.event_id, worker_id, str(exc))
            if isinstance(exc, QdrantIntegrationError):
                raise
            raise QdrantIntegrationError(
                f"PostgreSQL outbox publication failed: {type(exc).__name__}: {exc}"
            ) from exc
        upserted += counts["upserted"]
        deleted_count += counts["deleted"]
        unchanged += counts["unchanged"]

    alias_promoted = promote_qdrant_alias(client, config) if claimed else False
    try:
        remote_count = int(client.count(config.collection_name, exact=True).count)
    except Exception as exc:
        raise QdrantIntegrationError(f"could not count Qdrant after outbox drain: {exc}") from exc
    if max_events is None:
        remote_count = _reconcile_untracked_remote(engine, client, config, remote_count)
    return QdrantOutboxResult(
        collection_name=config.collection_name,
        claimed=claimed,
        upserted=upserted,
        deleted=deleted_count,
        unchanged=unchanged,
        remote_count=remote_count,
        collection_created=ensured.created,
        alias_promoted=alias_promoted,
    )


def queue_full_qdrant_reconciliation(
    engine: Engine,
    collection_name: str,
    *,
    reason: str,
) -> int:
    """Queue every canonical artwork after collection creation or count drift."""

    reconciliation_id = uuid7().hex
    inserted = 0
    with engine.connect().execution_options(stream_results=True) as reader:
        cursor = reader.execute(select(artworks.c.artwork_id).order_by(artworks.c.artwork_id))
        while rows := cursor.fetchmany(500):
            values = [
                {
                    "event_id": uuid7(),
                    "aggregate_type": "artwork",
                    "aggregate_id": str(row.artwork_id),
                    "event_type": EVENT_TYPE,
                    "payload": {
                        "artwork_id": str(row.artwork_id),
                        "collection_name": collection_name,
                        "reason": reason,
                    },
                    "idempotency_key": (
                        f"qdrant:{collection_name}:{row.artwork_id}:reconcile:{reconciliation_id}"
                    ),
                }
                for row in rows
            ]
            with engine.begin() as writer:
                returned = writer.execute(
                    postgresql_insert(outbox_events)
                    .values(values)
                    .on_conflict_do_nothing(index_elements=[outbox_events.c.idempotency_key])
                    .returning(outbox_events.c.event_id)
                ).fetchall()
            inserted += len(returned)
    return inserted


def _reconcile_untracked_remote(
    engine: Engine,
    client: Any,
    config: QdrantConfig,
    remote_count: int,
) -> int:
    with engine.connect() as connection:
        state_count = int(
            connection.execute(
                select(func.count())
                .select_from(vector_index_points)
                .where(vector_index_points.c.collection_name == config.collection_name)
            ).scalar_one()
        )
    if remote_count == state_count:
        return remote_count

    untracked: list[Any] = []
    offset: Any | None = None
    while True:
        records, next_offset = client.scroll(
            collection_name=config.collection_name,
            limit=config.batch_size,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        point_ids: list[tuple[Any, uuid.UUID | None]] = []
        for record in records:
            try:
                normalized = uuid.UUID(str(record.id))
            except ValueError:
                normalized = None
            point_ids.append((record.id, normalized))
        valid_ids = [normalized for _, normalized in point_ids if normalized is not None]
        if valid_ids:
            with engine.connect() as connection:
                expected = {
                    str(value)
                    for value in connection.execute(
                        select(vector_index_points.c.point_id).where(
                            vector_index_points.c.collection_name == config.collection_name,
                            vector_index_points.c.point_id.in_(valid_ids),
                        )
                    ).scalars()
                }
            untracked.extend(
                original
                for original, normalized in point_ids
                if normalized is None or str(normalized) not in expected
            )
        else:
            untracked.extend(original for original, _ in point_ids)
        if next_offset is None:
            break
        offset = next_offset
    for start in range(0, len(untracked), config.batch_size):
        client.delete(
            collection_name=config.collection_name,
            points_selector=untracked[start : start + config.batch_size],
            wait=True,
        )

    final_count = int(client.count(config.collection_name, exact=True).count)
    if final_count != state_count:
        raise QdrantIntegrationError(
            "Qdrant/PostgreSQL reconciliation count mismatch: "
            f"state={state_count}, remote={final_count}"
        )
    return final_count


def _reconcile_claim(
    engine: Engine,
    app_config: AppConfig,
    config: QdrantConfig,
    client: Any,
    worker_id: str,
    events: Sequence[OutboxEvent],
) -> dict[str, int]:
    for event in events:
        collection = str(event.payload.get("collection_name", ""))
        if collection != config.collection_name:
            raise QdrantIntegrationError(
                f"outbox event {event.event_id} targets {collection!r}, not "
                f"{config.collection_name!r}"
            )

    artwork_ids = sorted({event.aggregate_id for event in events})
    with engine.connect() as connection:
        rows = connection.execute(
            _eligible_statement(),
            {
                "artwork_ids": artwork_ids,
                "collection_name": config.collection_name,
                "require_sfw": config.require_sfw,
                "require_bluesky_accept": config.require_bluesky_accept,
                "clip_dimension": config.clip_dimension,
                "csd_dimension": config.csd_dimension,
                "clip_model_name": app_config.models.clip_model_name,
                "clip_model_version": app_config.models.clip_model_version,
                "csd_model_name": app_config.models.csd_model_name,
                "csd_model_version": app_config.models.csd_model_version,
            },
        ).mappings()
        eligible = {str(row["artwork_id"]): row for row in rows}

    points: list[Any] = []
    states: list[dict[str, object]] = []
    unchanged_ids: set[str] = set()
    for artwork_id, row in eligible.items():
        point, content_hash = _point_from_mapping(row, app_config, config)
        if row["indexed_content_hash"] == content_hash:
            unchanged_ids.add(artwork_id)
            continue
        points.append(point)
        states.append(
            {
                "collection_name": config.collection_name,
                "artwork_id": artwork_id,
                "point_id": uuid.UUID(point_id_for_artwork(artwork_id)),
                "content_hash": content_hash,
            }
        )

    ineligible_ids = sorted(set(artwork_ids) - set(eligible))
    if points:
        try:
            client.upsert(config.collection_name, points=points, wait=True)
        except Exception as exc:
            raise QdrantIntegrationError(f"Qdrant outbox upsert failed: {exc}") from exc
    if ineligible_ids:
        try:
            client.delete(
                collection_name=config.collection_name,
                points_selector=[point_id_for_artwork(value) for value in ineligible_ids],
                wait=True,
            )
        except Exception as exc:
            raise QdrantIntegrationError(f"Qdrant outbox deletion failed: {exc}") from exc

    event_ids = [event.event_id for event in events]
    with engine.begin() as connection:
        if states:
            statement = postgresql_insert(vector_index_points).values(states)
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        vector_index_points.c.collection_name,
                        vector_index_points.c.artwork_id,
                    ],
                    set_={
                        "point_id": statement.excluded.point_id,
                        "content_hash": statement.excluded.content_hash,
                        "indexed_at": func.now(),
                    },
                )
            )
        if ineligible_ids:
            connection.execute(
                delete(vector_index_points).where(
                    vector_index_points.c.collection_name == config.collection_name,
                    vector_index_points.c.artwork_id.in_(ineligible_ids),
                )
            )
        settled = connection.execute(
            update(outbox_events)
            .where(
                outbox_events.c.event_id.in_(event_ids),
                outbox_events.c.status == "processing",
                outbox_events.c.locked_by == worker_id,
            )
            .values(
                status="published",
                published_at=func.now(),
                locked_at=None,
                locked_by=None,
                last_error=None,
            )
        )
        if settled.rowcount != len(events):
            raise QdrantIntegrationError(
                f"outbox settlement lost ownership: claimed={len(events)}, settled={settled.rowcount}"
            )
    return {
        "upserted": len(points),
        "deleted": len(ineligible_ids),
        "unchanged": len(unchanged_ids),
    }


def _eligible_statement() -> Any:
    return text(
        """
        WITH decision_links AS (
          SELECT r.artwork_id, d.decision_key, d.decision,
                 d.accepted_for_main_corpus, d.predicted_class,
                 d.processed_at, 0 AS link_priority
            FROM ingest.artwork_filter_routes r
            JOIN ingest.artwork_filter_decisions d ON d.decision_key = r.decision_key
           WHERE r.artwork_id IS NOT NULL
          UNION ALL
          SELECT a.artwork_id, d.decision_key, d.decision,
                 d.accepted_for_main_corpus, d.predicted_class,
                 d.processed_at, 1 AS link_priority
            FROM catalog.artworks a
            JOIN ingest.artwork_filter_decisions d ON d.candidate_id = a.source_id
           WHERE a.source_platform = 'bluesky'
        ),
        ranked_decisions AS (
          SELECT decision_links.*,
                 row_number() OVER (
                   PARTITION BY artwork_id
                   ORDER BY processed_at DESC, decision_key DESC, link_priority
                 ) AS decision_rank
            FROM decision_links
        )
        SELECT a.artwork_id, a.artist_id, a.source_platform, a.review_status,
               a.is_sfw, a.demo_eligible, a.date_added,
               e.clip_vector, e.clip_dim, e.csd_style, e.csd_style_dim,
               e.date_computed,
               COALESCE(d.predicted_class, 'unknown') AS content_class,
               d.decision AS filter_decision,
               state.content_hash AS indexed_content_hash
          FROM catalog.artworks a
          JOIN ml.embeddings e ON e.artwork_id = a.artwork_id
          LEFT JOIN ranked_decisions d
            ON d.artwork_id = a.artwork_id AND d.decision_rank = 1
          LEFT JOIN ml.vector_index_points state
            ON state.artwork_id = a.artwork_id
           AND state.collection_name = :collection_name
         WHERE a.artwork_id = ANY(:artwork_ids)
           AND a.validated
           AND a.processed_path IS NOT NULL
           AND a.duplicate_of IS NULL
           AND a.demo_eligible
           AND (NOT :require_sfw OR a.is_sfw)
           AND (
             NOT :require_bluesky_accept
             OR a.source_platform != 'bluesky'
             OR (d.decision = 'accept' AND d.accepted_for_main_corpus)
           )
           AND e.clip_vector IS NOT NULL
           AND e.csd_style IS NOT NULL
           AND e.clip_dim = :clip_dimension
           AND e.csd_style_dim = :csd_dimension
           AND e.model_name_clip = :clip_model_name
           AND e.model_version_clip = :clip_model_version
           AND e.model_name_csd = :csd_model_name
           AND e.model_version_csd = :csd_model_version
        """
    ).bindparams(bindparam("artwork_ids", type_=ARRAY(Text)))


def _point_from_mapping(
    row: Mapping[str, Any],
    app_config: AppConfig,
    config: QdrantConfig,
) -> tuple[Any, str]:
    try:
        from qdrant_client import models as qmodels
    except ImportError as exc:
        raise QdrantIntegrationError(
            "Qdrant support requires `python -m pip install -e '.[qdrant]'`"
        ) from exc

    clip = _vector(row["clip_vector"], config.clip_dimension, "CLIP")
    style = _vector(row["csd_style"], config.csd_dimension, "CSD")
    payload = {
        "artwork_id": str(row["artwork_id"]),
        "artist_id": str(row["artist_id"]),
        "source_platform": str(row["source_platform"] or "unknown"),
        "content_class": str(row["content_class"] or "unknown"),
        "safety_status": "safe" if row["is_sfw"] is True else "unknown",
        "review_status": str(row["review_status"] or "unreviewed"),
        "demo_eligible": bool(row["demo_eligible"]),
        "created_at": _rfc3339(row["date_added"]),
        "model_bundle_version": model_bundle_version(app_config.models, config),
        "embedding_computed_at": _rfc3339(row["date_computed"]),
        "filter_decision": str(row["filter_decision"] or "manual"),
    }
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(clip, dtype=np.float32).tobytes())
    digest.update(np.ascontiguousarray(style, dtype=np.float32).tobytes())
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return (
        qmodels.PointStruct(
            id=point_id_for_artwork(str(row["artwork_id"])),
            vector={
                config.clip_vector_name: clip.tolist(),
                config.csd_vector_name: style.tolist(),
            },
            payload=payload,
        ),
        digest.hexdigest(),
    )


def _vector(value: Any, expected: int, name: str) -> np.ndarray:
    if isinstance(value, str):
        vector = np.fromstring(value.strip("[]"), sep=",", dtype=np.float32)
    else:
        vector = np.asarray(value, dtype=np.float32)
    if vector.shape != (expected,) or not np.isfinite(vector).all():
        raise QdrantIntegrationError(f"invalid {name} vector returned by PostgreSQL")
    return vector


def _rfc3339(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
