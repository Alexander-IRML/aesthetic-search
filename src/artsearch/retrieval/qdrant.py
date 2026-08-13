from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
import random
import sqlite3
import time
from typing import Any, Sequence
import uuid

import numpy as np

from artsearch.embed.storage import blob_to_vector, l2_normalize
from artsearch.ingest.config import AppConfig, ModelConfig
from artsearch.ingest.db import init_db
from artsearch.production.config import QdrantConfig
from artsearch.retrieval.search import _load_patch_matrices, patch_maxsim_score


POINT_NAMESPACE = uuid.UUID("f5270c98-c2f8-4b38-b5a2-cef92754210f")
PAYLOAD_INDEXES = {
    "artwork_id": "keyword",
    "artist_id": "keyword",
    "source_platform": "keyword",
    "content_class": "keyword",
    "safety_status": "keyword",
    "review_status": "keyword",
    "demo_eligible": "bool",
    "created_at": "datetime",
    "model_bundle_version": "keyword",
}


class QdrantIntegrationError(RuntimeError):
    """Raised when the derived Qdrant serving index cannot be used safely."""


@dataclass(frozen=True)
class CollectionEnsureResult:
    collection_name: str
    created: bool
    payload_indexes_created: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class QdrantSyncResult:
    collection_name: str
    alias_name: str
    eligible: int
    upserted: int
    unchanged: int
    deleted: int
    remote_untracked_deleted: int
    remote_count: int
    forced: bool
    collection_created: bool
    alias_promoted: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class QdrantSearchHit:
    artwork_id: str
    artist_id: str
    score: float
    rank: int
    content_class: str = "unknown"
    fusion_score: float | None = None
    fusion_rank: int | None = None
    clip_score: float | None = None
    clip_rank: int | None = None
    dino_score: float | None = None
    dino_rank: int | None = None
    patch_score: float | None = None
    patch_rank: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class VectorAnnMetrics:
    vector_name: str
    query_count: int
    top_k: int
    mean_recall: float
    minimum_recall: float
    approximate_latency_p50_ms: float
    approximate_latency_p95_ms: float
    exact_latency_p50_ms: float
    exact_latency_p95_ms: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class QdrantAnnEvaluation:
    collection_name: str
    alias_name: str
    eligible_count: int
    sampled_queries: int
    seed: int
    metrics: tuple[VectorAnnMetrics, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["metrics"] = [metric.to_dict() for metric in self.metrics]
        return payload


def build_qdrant_client(config: QdrantConfig) -> Any:
    """Build a client without placing endpoint credentials in versioned config."""

    if not config.enabled:
        raise QdrantIntegrationError("Qdrant is disabled in the production configuration")
    QdrantClient, _ = _qdrant_imports()
    url = os.environ.get(config.url_env, "").strip() or config.url.strip()
    if not url:
        raise QdrantIntegrationError(f"Qdrant URL is missing; set {config.url_env} or qdrant.url")
    api_key = os.environ.get(config.api_key_env, "").strip() or None
    if config.require_api_key and api_key is None:
        raise QdrantIntegrationError(f"Qdrant API key is missing from {config.api_key_env}")
    if url == ":memory:":
        return QdrantClient(location=":memory:")
    return QdrantClient(
        url=url,
        api_key=api_key,
        prefer_grpc=config.prefer_grpc,
        timeout=config.timeout_seconds,
    )


def model_bundle_version(models: ModelConfig, config: QdrantConfig) -> str:
    payload = {
        "clip_model": models.clip_model_name,
        "clip_revision": models.clip_model_version,
        "clip_dimension": config.clip_dimension,
        "clip_vector": config.clip_vector_name,
        "dino_model": models.dino_model_name,
        "dino_revision": models.dino_model_version,
        "dino_dimension": config.dino_dimension,
        "dino_vector": config.dino_vector_name,
        "datatype": config.datatype,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def point_id_for_artwork(artwork_id: str) -> str:
    if not artwork_id:
        raise ValueError("artwork_id must not be empty")
    return str(uuid.uuid5(POINT_NAMESPACE, artwork_id))


def ensure_qdrant_collection(
    client: Any,
    config: QdrantConfig,
    models: ModelConfig,
) -> CollectionEnsureResult:
    """Create or validate one immutable model-version collection schema."""

    _, qmodels = _qdrant_imports()
    created = False
    try:
        exists = bool(client.collection_exists(config.collection_name))
        if not exists:
            client.create_collection(
                collection_name=config.collection_name,
                vectors_config={
                    config.clip_vector_name: qmodels.VectorParams(
                        size=config.clip_dimension,
                        distance=qmodels.Distance.COSINE,
                        datatype=_datatype(qmodels, config.datatype),
                        on_disk=config.on_disk_vectors,
                    ),
                    config.dino_vector_name: qmodels.VectorParams(
                        size=config.dino_dimension,
                        distance=qmodels.Distance.COSINE,
                        datatype=_datatype(qmodels, config.datatype),
                        on_disk=config.on_disk_vectors,
                    ),
                },
                shard_number=1,
                replication_factor=1,
                on_disk_payload=config.on_disk_payload,
                metadata={
                    "schema": "artsearch-qdrant-v1",
                    "model_bundle_version": model_bundle_version(models, config),
                    "clip_model": models.clip_model_name,
                    "clip_revision": models.clip_model_version,
                    "dino_model": models.dino_model_name,
                    "dino_revision": models.dino_model_version,
                },
            )
            created = True

        info = client.get_collection(config.collection_name)
        _validate_collection_schema(info, config, models)
        indexed_fields = set((info.payload_schema or {}).keys())
        created_indexes = 0
        for field_name, field_type in PAYLOAD_INDEXES.items():
            if field_name in indexed_fields:
                continue
            client.create_payload_index(
                collection_name=config.collection_name,
                field_name=field_name,
                field_schema=_payload_schema(qmodels, field_type),
                wait=True,
            )
            created_indexes += 1
        return CollectionEnsureResult(
            collection_name=config.collection_name,
            created=created,
            payload_indexes_created=created_indexes,
        )
    except (QdrantIntegrationError, ValueError):
        raise
    except Exception as exc:
        raise QdrantIntegrationError(
            f"could not ensure Qdrant collection {config.collection_name!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def promote_qdrant_alias(client: Any, config: QdrantConfig) -> bool:
    """Atomically point the stable read alias at the configured physical collection."""

    _, qmodels = _qdrant_imports()
    try:
        aliases = {
            alias.alias_name: alias.collection_name for alias in client.get_aliases().aliases
        }
        current = aliases.get(config.alias_name)
        if current == config.collection_name:
            return False
        operations: list[Any] = []
        if current is not None:
            operations.append(
                qmodels.DeleteAliasOperation(
                    delete_alias=qmodels.DeleteAlias(alias_name=config.alias_name)
                )
            )
        operations.append(
            qmodels.CreateAliasOperation(
                create_alias=qmodels.CreateAlias(
                    collection_name=config.collection_name,
                    alias_name=config.alias_name,
                )
            )
        )
        client.update_collection_aliases(operations)
        return True
    except Exception as exc:
        raise QdrantIntegrationError(
            f"could not promote Qdrant alias {config.alias_name!r}: {type(exc).__name__}: {exc}"
        ) from exc


def sync_qdrant_from_sqlite(
    connection: sqlite3.Connection,
    app_config: AppConfig,
    config: QdrantConfig,
    client: Any,
    *,
    force: bool = False,
    prune: bool = True,
    promote: bool | None = None,
) -> QdrantSyncResult:
    """Incrementally publish the canonical safe corpus into a derived serving index."""

    init_db(connection)
    ensured = ensure_qdrant_collection(client, config, app_config.models)
    if ensured.created:
        connection.execute(
            "DELETE FROM vector_index_points WHERE collection_name = ?",
            (config.collection_name,),
        )
        connection.commit()

    state_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM vector_index_points WHERE collection_name = ?",
            (config.collection_name,),
        ).fetchone()[0]
    )

    try:
        initial_remote_count = int(client.count(config.collection_name, exact=True).count)
    except Exception as exc:
        raise QdrantIntegrationError(
            f"could not count Qdrant collection {config.collection_name!r}: {exc}"
        ) from exc
    forced = force or initial_remote_count != state_count

    connection.execute("DROP TABLE IF EXISTS temp.qdrant_eligible_points")
    connection.execute(
        """
        CREATE TEMP TABLE qdrant_eligible_points (
            artwork_id TEXT PRIMARY KEY,
            point_id TEXT NOT NULL UNIQUE
        )
        """
    )

    eligible = upserted = unchanged = deleted = remote_untracked_deleted = 0
    cursor = _eligible_rows(connection, app_config, config)
    while rows := cursor.fetchmany(config.batch_size):
        pending: list[tuple[Any, str, str, str]] = []
        for row in rows:
            eligible += 1
            point_id = point_id_for_artwork(str(row["artwork_id"]))
            connection.execute(
                "INSERT INTO temp.qdrant_eligible_points (artwork_id, point_id) VALUES (?, ?)",
                (row["artwork_id"], point_id),
            )
            point, content_hash = _point_from_row(
                row,
                point_id,
                app_config.models,
                config,
            )
            if not forced and row["indexed_content_hash"] == content_hash:
                unchanged += 1
                continue
            pending.append((point, str(row["artwork_id"]), point_id, content_hash))
        if pending:
            _upsert_points(connection, client, config, pending)
            upserted += len(pending)

    if prune:
        stale_cursor = connection.execute(
            """
            SELECT state.artwork_id, state.point_id
              FROM vector_index_points AS state
              LEFT JOIN temp.qdrant_eligible_points AS eligible
                ON eligible.artwork_id = state.artwork_id
             WHERE state.collection_name = ?
               AND eligible.artwork_id IS NULL
             ORDER BY state.artwork_id
            """,
            (config.collection_name,),
        )
        while rows := stale_cursor.fetchmany(config.batch_size):
            point_ids = [str(row["point_id"]) for row in rows]
            _delete_qdrant_points(client, config.collection_name, point_ids)
            connection.executemany(
                """
                DELETE FROM vector_index_points
                 WHERE collection_name = ? AND artwork_id = ?
                """,
                [(config.collection_name, str(row["artwork_id"])) for row in rows],
            )
            connection.commit()
            deleted += len(rows)

    remote_count = int(client.count(config.collection_name, exact=True).count)
    if prune and remote_count != eligible:
        remote_untracked_deleted = _delete_untracked_remote_points(
            connection,
            client,
            config,
        )
        remote_count = int(client.count(config.collection_name, exact=True).count)
    if remote_count != eligible:
        raise QdrantIntegrationError(
            "Qdrant reconciliation count mismatch: "
            f"eligible={eligible}, remote={remote_count}, collection={config.collection_name}"
        )

    should_promote = config.promote_after_sync if promote is None else promote
    alias_promoted = promote_qdrant_alias(client, config) if should_promote else False
    return QdrantSyncResult(
        collection_name=config.collection_name,
        alias_name=config.alias_name,
        eligible=eligible,
        upserted=upserted,
        unchanged=unchanged,
        deleted=deleted,
        remote_untracked_deleted=remote_untracked_deleted,
        remote_count=remote_count,
        forced=forced,
        collection_created=ensured.created,
        alias_promoted=alias_promoted,
    )


def evaluate_qdrant_ann(
    connection: sqlite3.Connection,
    app_config: AppConfig,
    config: QdrantConfig,
    client: Any,
    *,
    sample_size: int = 50,
    top_k: int = 20,
    seed: int = 0,
) -> QdrantAnnEvaluation:
    """Measure approximate named-vector recall against Qdrant exact search."""

    if sample_size <= 0 or top_k <= 0:
        raise ValueError("sample_size and top_k must be positive")
    init_db(connection)
    ensure_qdrant_collection(client, config, app_config.models)
    rows = _eligible_rows(connection, app_config, config).fetchall()
    if len(rows) < 2:
        raise QdrantIntegrationError(
            "Qdrant ANN evaluation requires at least two eligible artworks"
        )
    selected = random.Random(seed).sample(rows, min(sample_size, len(rows)))
    effective_top_k = min(top_k, len(rows) - 1)
    vectors = (
        (config.clip_vector_name, "clip_vector", "clip_dim", config.clip_dimension),
        (config.dino_vector_name, "dino_pooled", "dino_pooled_dim", config.dino_dimension),
    )
    metrics = tuple(
        _evaluate_named_vector(
            client,
            config,
            selected,
            vector_name=vector_name,
            blob_column=blob_column,
            dimension_column=dimension_column,
            expected_dimension=expected_dimension,
            top_k=effective_top_k,
        )
        for vector_name, blob_column, dimension_column, expected_dimension in vectors
    )
    return QdrantAnnEvaluation(
        collection_name=config.collection_name,
        alias_name=config.alias_name,
        eligible_count=len(rows),
        sampled_queries=len(selected),
        seed=seed,
        metrics=metrics,
    )


class QdrantSearchService:
    """Search named global vectors in Qdrant and rerank compact patches locally."""

    def __init__(self, client: Any, config: QdrantConfig) -> None:
        self.client = client
        self.config = config

    def search_by_artwork(
        self,
        connection: sqlite3.Connection,
        app_config: AppConfig,
        artwork_id: str,
        *,
        top_k: int = 20,
        include_query_artist: bool = True,
        use_patch_rerank: bool = True,
    ) -> list[QdrantSearchHit]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        row = connection.execute(
            """
            SELECT a.artist_id, e.clip_vector, e.clip_dim,
                   e.dino_pooled, e.dino_pooled_dim
              FROM artworks AS a
              JOIN embeddings AS e ON e.artwork_id = a.artwork_id
             WHERE a.artwork_id = ?
               AND e.model_name_clip = ?
               AND e.model_version_clip = ?
               AND e.model_name_dino = ?
               AND e.model_version_dino = ?
            """,
            (
                artwork_id,
                app_config.models.clip_model_name,
                app_config.models.clip_model_version,
                app_config.models.dino_model_name,
                app_config.models.dino_model_version,
            ),
        ).fetchone()
        if row is None:
            raise ValueError(f"current CLIP/DINO embeddings not found for {artwork_id!r}")
        clip = _vector_from_blob(row["clip_vector"], row["clip_dim"], self.config.clip_dimension)
        dino = _vector_from_blob(
            row["dino_pooled"],
            row["dino_pooled_dim"],
            self.config.dino_dimension,
        )
        hits = self._search_fused(
            clip,
            dino,
            exclude_artwork_id=artwork_id,
            exclude_artist_id=(None if include_query_artist else str(row["artist_id"])),
        )
        if use_patch_rerank and self.config.patch_rerank_limit:
            hits = _rerank_patches(
                connection,
                app_config,
                artwork_id,
                hits,
                limit=self.config.patch_rerank_limit,
                patch_match_top_n=app_config.retrieval.patch_match_top_n,
            )
        return _apply_artist_diversity(hits, top_k, self.config.max_results_per_artist)

    def search_by_clip_vector(
        self,
        vector: np.ndarray,
        *,
        top_k: int = 20,
    ) -> list[QdrantSearchHit]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        clip = _validated_vector(vector, self.config.clip_dimension)
        _, qmodels = _qdrant_imports()
        query_filter = _search_filter(qmodels)
        limit = max(top_k * self.config.max_results_per_artist, top_k)
        try:
            response = self.client.query_points(
                collection_name=self.config.alias_name,
                query=clip.tolist(),
                using=self.config.clip_vector_name,
                query_filter=query_filter,
                search_params=qmodels.SearchParams(hnsw_ef=self.config.hnsw_ef),
                limit=limit,
                with_payload=True,
            )
        except Exception as exc:
            raise QdrantIntegrationError(
                f"Qdrant CLIP search failed: {type(exc).__name__}: {exc}"
            ) from exc
        hits = [
            QdrantSearchHit(
                artwork_id=_payload_text(point.payload, "artwork_id"),
                artist_id=_payload_text(point.payload, "artist_id"),
                content_class=_payload_text(point.payload, "content_class", "unknown"),
                score=float(point.score),
                rank=rank,
                clip_score=float(point.score),
                clip_rank=rank,
            )
            for rank, point in enumerate(response.points, start=1)
        ]
        return _apply_artist_diversity(hits, top_k, self.config.max_results_per_artist)

    def _search_fused(
        self,
        clip: np.ndarray,
        dino: np.ndarray,
        *,
        exclude_artwork_id: str,
        exclude_artist_id: str | None,
    ) -> list[QdrantSearchHit]:
        _, qmodels = _qdrant_imports()
        query_filter = _search_filter(
            qmodels,
            exclude_artwork_id=exclude_artwork_id,
            exclude_artist_id=exclude_artist_id,
        )
        params = qmodels.SearchParams(hnsw_ef=self.config.hnsw_ef)
        clip_prefetch = qmodels.Prefetch(
            query=clip.tolist(),
            using=self.config.clip_vector_name,
            filter=query_filter,
            params=params,
            limit=self.config.prefetch_limit,
        )
        dino_prefetch = qmodels.Prefetch(
            query=dino.tolist(),
            using=self.config.dino_vector_name,
            filter=query_filter,
            params=params,
            limit=self.config.prefetch_limit,
        )
        requests = [
            qmodels.QueryRequest(
                query=clip.tolist(),
                using=self.config.clip_vector_name,
                filter=query_filter,
                params=params,
                limit=self.config.prefetch_limit,
                with_payload=True,
            ),
            qmodels.QueryRequest(
                query=dino.tolist(),
                using=self.config.dino_vector_name,
                filter=query_filter,
                params=params,
                limit=self.config.prefetch_limit,
                with_payload=True,
            ),
            qmodels.QueryRequest(
                prefetch=[clip_prefetch, dino_prefetch],
                query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
                filter=query_filter,
                limit=self.config.fusion_limit,
                with_payload=True,
            ),
        ]
        try:
            clip_response, dino_response, fused_response = self.client.query_batch_points(
                collection_name=self.config.alias_name,
                requests=requests,
            )
        except Exception as exc:
            raise QdrantIntegrationError(
                f"Qdrant CLIP/DINO fusion search failed: {type(exc).__name__}: {exc}"
            ) from exc

        clip_evidence = _rank_evidence(clip_response.points)
        dino_evidence = _rank_evidence(dino_response.points)
        hits: list[QdrantSearchHit] = []
        for fusion_rank, point in enumerate(fused_response.points, start=1):
            artwork_id = _payload_text(point.payload, "artwork_id")
            clip_score, clip_rank = clip_evidence.get(artwork_id, (None, None))
            dino_score, dino_rank = dino_evidence.get(artwork_id, (None, None))
            hits.append(
                QdrantSearchHit(
                    artwork_id=artwork_id,
                    artist_id=_payload_text(point.payload, "artist_id"),
                    content_class=_payload_text(point.payload, "content_class", "unknown"),
                    score=float(point.score),
                    rank=fusion_rank,
                    fusion_score=float(point.score),
                    fusion_rank=fusion_rank,
                    clip_score=clip_score,
                    clip_rank=clip_rank,
                    dino_score=dino_score,
                    dino_rank=dino_rank,
                )
            )
        return hits


def _eligible_rows(
    connection: sqlite3.Connection,
    app_config: AppConfig,
    config: QdrantConfig,
) -> sqlite3.Cursor:
    return connection.execute(
        """
        WITH decision_links AS (
            SELECT r.artwork_id,
                   d.decision_key,
                   d.decision,
                   d.accepted_for_main_corpus,
                   d.predicted_class,
                   d.processed_at,
                   0 AS link_priority
              FROM artwork_filter_routes AS r
              JOIN artwork_filter_decisions AS d
                ON d.decision_key = r.decision_key
             WHERE r.artwork_id IS NOT NULL
            UNION ALL
            SELECT a.artwork_id,
                   d.decision_key,
                   d.decision,
                   d.accepted_for_main_corpus,
                   d.predicted_class,
                   d.processed_at,
                   1 AS link_priority
              FROM artworks AS a
              JOIN artwork_filter_decisions AS d
                ON d.candidate_id = a.source_id
             WHERE a.source_platform = 'bluesky'
        ),
        ranked_decisions AS (
            SELECT decision_links.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY artwork_id
                       ORDER BY processed_at DESC, decision_key DESC, link_priority
                   ) AS decision_rank
              FROM decision_links
        )
        SELECT a.artwork_id,
               a.artist_id,
               a.source_platform,
               a.review_status,
               a.is_sfw,
               a.demo_eligible,
               a.date_added,
               e.clip_vector,
               e.clip_dim,
               e.dino_pooled,
               e.dino_pooled_dim,
               e.date_computed,
               COALESCE(d.predicted_class, 'unknown') AS content_class,
               d.decision AS filter_decision,
               state.content_hash AS indexed_content_hash
          FROM artworks AS a
          JOIN embeddings AS e ON e.artwork_id = a.artwork_id
          LEFT JOIN ranked_decisions AS d
            ON d.artwork_id = a.artwork_id AND d.decision_rank = 1
          LEFT JOIN vector_index_points AS state
            ON state.artwork_id = a.artwork_id AND state.collection_name = ?
         WHERE a.validated = 1
           AND a.processed_path IS NOT NULL
           AND a.duplicate_of IS NULL
           AND a.demo_eligible = 1
           AND (? = 0 OR a.is_sfw = 1)
           AND (
               ? = 0
               OR a.source_platform != 'bluesky'
               OR (d.decision = 'accept' AND d.accepted_for_main_corpus = 1)
           )
           AND e.clip_vector IS NOT NULL
           AND e.dino_pooled IS NOT NULL
           AND e.clip_dim = ?
           AND e.dino_pooled_dim = ?
           AND e.model_name_clip = ?
           AND e.model_version_clip = ?
           AND e.model_name_dino = ?
           AND e.model_version_dino = ?
         ORDER BY a.artwork_id
        """,
        (
            config.collection_name,
            int(config.require_sfw),
            int(config.require_bluesky_accept),
            config.clip_dimension,
            config.dino_dimension,
            app_config.models.clip_model_name,
            app_config.models.clip_model_version,
            app_config.models.dino_model_name,
            app_config.models.dino_model_version,
        ),
    )


def _evaluate_named_vector(
    client: Any,
    config: QdrantConfig,
    rows: Sequence[sqlite3.Row],
    *,
    vector_name: str,
    blob_column: str,
    dimension_column: str,
    expected_dimension: int,
    top_k: int,
) -> VectorAnnMetrics:
    _, qmodels = _qdrant_imports()
    recalls: list[float] = []
    approximate_latencies: list[float] = []
    exact_latencies: list[float] = []
    for row in rows:
        query = _vector_from_blob(
            row[blob_column],
            row[dimension_column],
            expected_dimension,
        ).tolist()
        query_filter = _search_filter(
            qmodels,
            exclude_artwork_id=str(row["artwork_id"]),
        )
        started = time.perf_counter()
        approximate = client.query_points(
            collection_name=config.alias_name,
            query=query,
            using=vector_name,
            query_filter=query_filter,
            search_params=qmodels.SearchParams(hnsw_ef=config.hnsw_ef),
            limit=top_k,
            with_payload=True,
        )
        approximate_latencies.append((time.perf_counter() - started) * 1000.0)

        started = time.perf_counter()
        exact = client.query_points(
            collection_name=config.collection_name,
            query=query,
            using=vector_name,
            query_filter=query_filter,
            search_params=qmodels.SearchParams(exact=True),
            limit=top_k,
            with_payload=True,
        )
        exact_latencies.append((time.perf_counter() - started) * 1000.0)
        approximate_ids = {
            _payload_text(point.payload, "artwork_id") for point in approximate.points
        }
        exact_ids = {_payload_text(point.payload, "artwork_id") for point in exact.points}
        recalls.append(len(approximate_ids & exact_ids) / len(exact_ids) if exact_ids else 1.0)

    return VectorAnnMetrics(
        vector_name=vector_name,
        query_count=len(rows),
        top_k=top_k,
        mean_recall=float(np.mean(recalls)),
        minimum_recall=float(np.min(recalls)),
        approximate_latency_p50_ms=_percentile(approximate_latencies, 50),
        approximate_latency_p95_ms=_percentile(approximate_latencies, 95),
        exact_latency_p50_ms=_percentile(exact_latencies, 50),
        exact_latency_p95_ms=_percentile(exact_latencies, 95),
    )


def _point_from_row(
    row: sqlite3.Row,
    point_id: str,
    models: ModelConfig,
    config: QdrantConfig,
) -> tuple[Any, str]:
    _, qmodels = _qdrant_imports()
    clip = _vector_from_blob(row["clip_vector"], row["clip_dim"], config.clip_dimension)
    dino = _vector_from_blob(
        row["dino_pooled"],
        row["dino_pooled_dim"],
        config.dino_dimension,
    )
    payload = {
        "artwork_id": str(row["artwork_id"]),
        "artist_id": str(row["artist_id"]),
        "source_platform": str(row["source_platform"] or "unknown"),
        "content_class": str(row["content_class"] or "unknown"),
        "safety_status": "safe" if row["is_sfw"] == 1 else "unknown",
        "review_status": str(row["review_status"] or "unreviewed"),
        "demo_eligible": bool(row["demo_eligible"]),
        "created_at": _rfc3339(row["date_added"]),
        "model_bundle_version": model_bundle_version(models, config),
        "embedding_computed_at": _rfc3339(row["date_computed"]),
        "filter_decision": str(row["filter_decision"] or "manual"),
    }
    content_hash = hashlib.sha256()
    content_hash.update(bytes(row["clip_vector"]))
    content_hash.update(bytes(row["dino_pooled"]))
    content_hash.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return (
        qmodels.PointStruct(
            id=point_id,
            vector={
                config.clip_vector_name: clip.tolist(),
                config.dino_vector_name: dino.tolist(),
            },
            payload=payload,
        ),
        content_hash.hexdigest(),
    )


def _upsert_points(
    connection: sqlite3.Connection,
    client: Any,
    config: QdrantConfig,
    pending: Sequence[tuple[Any, str, str, str]],
) -> None:
    try:
        client.upsert(
            collection_name=config.collection_name,
            points=[item[0] for item in pending],
            wait=True,
        )
    except Exception as exc:
        raise QdrantIntegrationError(
            f"Qdrant batch upsert failed: {type(exc).__name__}: {exc}"
        ) from exc
    connection.executemany(
        """
        INSERT INTO vector_index_points (
            collection_name, artwork_id, point_id, content_hash
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(collection_name, artwork_id) DO UPDATE SET
            point_id = excluded.point_id,
            content_hash = excluded.content_hash,
            indexed_at = CURRENT_TIMESTAMP
        """,
        [
            (config.collection_name, artwork_id, point_id, content_hash)
            for _, artwork_id, point_id, content_hash in pending
        ],
    )
    connection.commit()


def _delete_qdrant_points(client: Any, collection_name: str, point_ids: list[str]) -> None:
    if not point_ids:
        return
    try:
        client.delete(
            collection_name=collection_name,
            points_selector=point_ids,
            wait=True,
        )
    except Exception as exc:
        raise QdrantIntegrationError(
            f"Qdrant point deletion failed: {type(exc).__name__}: {exc}"
        ) from exc


def _delete_untracked_remote_points(
    connection: sqlite3.Connection,
    client: Any,
    config: QdrantConfig,
) -> int:
    untracked_ids: list[str] = []
    offset: Any | None = None
    while True:
        records, next_offset = client.scroll(
            collection_name=config.collection_name,
            limit=config.batch_size,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        point_ids = [str(record.id) for record in records]
        if point_ids:
            placeholders = ", ".join("?" for _ in point_ids)
            expected = {
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT point_id
                      FROM temp.qdrant_eligible_points
                     WHERE point_id IN ({placeholders})
                    """,
                    point_ids,
                ).fetchall()
            }
            untracked_ids.extend(point_id for point_id in point_ids if point_id not in expected)
        if next_offset is None:
            break
        offset = next_offset
    for offset in range(0, len(untracked_ids), config.batch_size):
        _delete_qdrant_points(
            client,
            config.collection_name,
            untracked_ids[offset : offset + config.batch_size],
        )
    return len(untracked_ids)


def _rerank_patches(
    connection: sqlite3.Connection,
    app_config: AppConfig,
    query_artwork_id: str,
    hits: list[QdrantSearchHit],
    *,
    limit: int,
    patch_match_top_n: int,
) -> list[QdrantSearchHit]:
    shortlist = hits[:limit]
    matrices = _load_patch_matrices(
        connection,
        app_config,
        [query_artwork_id, *(hit.artwork_id for hit in shortlist)],
    )
    query_patches = matrices.get(query_artwork_id)
    if query_patches is None:
        return hits
    scored: list[QdrantSearchHit] = []
    for hit in shortlist:
        candidate = matrices.get(hit.artwork_id)
        patch_score = (
            patch_maxsim_score(query_patches, candidate, top_n=patch_match_top_n)
            if candidate is not None
            else None
        )
        scored.append(replace(hit, patch_score=patch_score))
    scored.sort(
        key=lambda hit: (
            hit.patch_score is None,
            -(hit.patch_score if hit.patch_score is not None else -1.0),
            hit.fusion_rank or 0,
            hit.artwork_id,
        )
    )
    reranked = [
        replace(
            hit,
            score=hit.patch_score if hit.patch_score is not None else hit.score,
            patch_rank=rank if hit.patch_score is not None else None,
        )
        for rank, hit in enumerate(scored, start=1)
    ]
    return [*reranked, *hits[limit:]]


def _apply_artist_diversity(
    hits: Sequence[QdrantSearchHit],
    top_k: int,
    max_per_artist: int,
) -> list[QdrantSearchHit]:
    counts: dict[str, int] = {}
    selected: list[QdrantSearchHit] = []
    for hit in hits:
        count = counts.get(hit.artist_id, 0)
        if count >= max_per_artist:
            continue
        counts[hit.artist_id] = count + 1
        selected.append(replace(hit, rank=len(selected) + 1))
        if len(selected) >= top_k:
            break
    return selected


def _search_filter(
    qmodels: Any,
    *,
    exclude_artwork_id: str | None = None,
    exclude_artist_id: str | None = None,
) -> Any:
    must = [
        qmodels.FieldCondition(
            key="demo_eligible",
            match=qmodels.MatchValue(value=True),
        ),
        qmodels.FieldCondition(
            key="safety_status",
            match=qmodels.MatchValue(value="safe"),
        ),
    ]
    must_not = []
    if exclude_artwork_id is not None:
        must_not.append(
            qmodels.FieldCondition(
                key="artwork_id",
                match=qmodels.MatchValue(value=exclude_artwork_id),
            )
        )
    if exclude_artist_id is not None:
        must_not.append(
            qmodels.FieldCondition(
                key="artist_id",
                match=qmodels.MatchValue(value=exclude_artist_id),
            )
        )
    return qmodels.Filter(must=must, must_not=must_not or None)


def _rank_evidence(points: Sequence[Any]) -> dict[str, tuple[float, int]]:
    return {
        _payload_text(point.payload, "artwork_id"): (float(point.score), rank)
        for rank, point in enumerate(points, start=1)
    }


def _payload_text(payload: dict[str, Any] | None, key: str, default: str = "") -> str:
    value = (payload or {}).get(key, default)
    if not isinstance(value, str) or not value:
        if default:
            return default
        raise QdrantIntegrationError(f"Qdrant result payload is missing {key!r}")
    return value


def _vector_from_blob(blob: bytes, stored_dim: int, expected_dim: int) -> np.ndarray:
    if int(stored_dim) != expected_dim:
        raise ValueError(
            f"embedding dimension mismatch: stored={stored_dim}, expected={expected_dim}"
        )
    return _validated_vector(blob_to_vector(blob, int(stored_dim)), expected_dim)


def _validated_vector(vector: np.ndarray, expected_dim: int) -> np.ndarray:
    values = np.asarray(vector, dtype=np.float32).reshape(-1)
    if values.shape != (expected_dim,):
        raise ValueError(
            f"embedding shape mismatch: actual={values.shape}, expected=({expected_dim},)"
        )
    if not np.isfinite(values).all():
        raise ValueError("embedding contains non-finite values")
    normalized = l2_normalize(values)
    if not np.any(normalized):
        raise ValueError("embedding has zero norm")
    return normalized


def _percentile(values: Sequence[float], percentile: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))


def _rfc3339(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Qdrant payload timestamp must not be empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid Qdrant payload timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_collection_schema(
    info: Any,
    config: QdrantConfig,
    models: ModelConfig,
) -> None:
    vectors = info.config.params.vectors
    if not isinstance(vectors, dict):
        raise QdrantIntegrationError("Qdrant collection does not use named vectors")
    expected = {
        config.clip_vector_name: config.clip_dimension,
        config.dino_vector_name: config.dino_dimension,
    }
    if set(vectors) != set(expected):
        raise QdrantIntegrationError(
            f"Qdrant named-vector mismatch: actual={sorted(vectors)}, expected={sorted(expected)}"
        )
    for name, dimension in expected.items():
        params = vectors[name]
        if int(params.size) != dimension or str(params.distance.value) != "Cosine":
            raise QdrantIntegrationError(
                f"Qdrant vector schema mismatch for {name!r}: "
                f"size={params.size}, distance={params.distance}"
            )
        actual_datatype = params.datatype.value if params.datatype is not None else "float32"
        if actual_datatype != config.datatype:
            raise QdrantIntegrationError(
                f"Qdrant datatype mismatch for {name!r}: "
                f"actual={actual_datatype}, expected={config.datatype}"
            )
    metadata = info.config.metadata or {}
    expected_bundle = model_bundle_version(models, config)
    if metadata.get("schema") != "artsearch-qdrant-v1":
        raise QdrantIntegrationError("Qdrant collection metadata schema is incompatible")
    if metadata.get("model_bundle_version") != expected_bundle:
        raise QdrantIntegrationError(
            "Qdrant collection model bundle is incompatible; configure a new physical "
            "collection name before syncing"
        )


def _datatype(qmodels: Any, value: str) -> Any:
    return qmodels.Datatype.FLOAT16 if value == "float16" else qmodels.Datatype.FLOAT32


def _payload_schema(qmodels: Any, value: str) -> Any:
    return {
        "keyword": qmodels.PayloadSchemaType.KEYWORD,
        "bool": qmodels.PayloadSchemaType.BOOL,
        "datetime": qmodels.PayloadSchemaType.DATETIME,
    }[value]


def _qdrant_imports() -> tuple[Any, Any]:
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        raise QdrantIntegrationError(
            "Qdrant integration requires: pip install -e '.[qdrant]'"
        ) from exc
    return QdrantClient, models
