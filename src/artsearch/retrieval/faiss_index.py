from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import threading
import time
from typing import Any, Sequence
import uuid

import numpy as np

from artsearch.embed.storage import blob_to_vector, l2_normalize
from artsearch.ingest.config import AppConfig, ModelConfig
from artsearch.ingest.db import connect, init_db
from artsearch.production.capacity import CAPACITY_PLAN_VERSION
from artsearch.production.config import FaissConfig
from artsearch.retrieval.backend import RetrievalContract, SearchHit
from artsearch.retrieval.corpus import eligible_embedding_count, eligible_embedding_rows


SNAPSHOT_SCHEMA_VERSION = "artsearch-faiss-snapshot-v1"
EVALUATION_SCHEMA_VERSION = "artsearch-faiss-evaluation-v1"
_SNAPSHOT_FILES = {
    "clip_index": "clip.faiss",
    "csd_index": "csd.faiss",
    "clip_rerank": "clip.f16.npy",
    "csd_rerank": "csd.f16.npy",
    "catalog": "catalog.sqlite3",
}


class FaissIndexError(RuntimeError):
    """Raised when a FAISS snapshot cannot be built, validated, or searched."""


class FaissDependencyError(FaissIndexError):
    """Raised when the optional FAISS runtime is unavailable."""


@dataclass(frozen=True)
class FaissSnapshotBuildResult:
    snapshot_version: str
    snapshot_path: str
    point_count: int
    index_kind: str
    nlist: int
    nprobe: int
    training_sample_size: int
    byte_size: int
    duration_ms: float
    model_bundle_version: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FaissVectorMetrics:
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
class FaissEvaluationReport:
    snapshot_version: str
    point_count: int
    sampled_queries: int
    seed: int
    search_latency_p50_ms: float
    search_latency_p95_ms: float
    metrics: tuple[FaissVectorMetrics, ...]
    minimum_mean_recall_required: float
    maximum_search_p95_ms_allowed: float
    quality_gate_passed: bool
    schema_version: str = EVALUATION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["metrics"] = [metric.to_dict() for metric in self.metrics]
        return payload


def faiss_model_bundle_version(models: ModelConfig, config: FaissConfig) -> str:
    payload = {
        "clip_model": models.clip_model_name,
        "clip_revision": models.clip_model_version,
        "clip_dimension": config.clip_dimension,
        "csd_model": models.csd_model_name,
        "csd_revision": models.csd_model_version,
        "csd_dimension": config.csd_dimension,
        "normalization": "l2-v1",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_faiss_snapshot(
    database_path: str | Path,
    app_config: AppConfig,
    config: FaissConfig,
    *,
    snapshot_version: str,
    production_config_hash: str = "",
    force_flat: bool = False,
) -> FaissSnapshotBuildResult:
    """Build one immutable snapshot without loading the complete corpus into RAM."""

    faiss = _faiss_import()
    version = _safe_snapshot_version(snapshot_version)
    snapshots_root = config.artifact_root / "snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)
    destination = snapshots_root / version
    if destination.exists():
        raise FaissIndexError(f"FAISS snapshot already exists: {destination}")
    temporary = snapshots_root / f".{version}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    started = time.perf_counter()

    try:
        with connect(database_path) as connection:
            init_db(connection)
            # Keep count, training, and population on one corpus view. Large
            # production builds should still pause writers to bound WAL growth.
            connection.execute("BEGIN")
            point_count = eligible_embedding_count(
                connection,
                app_config.models,
                clip_dimension=config.clip_dimension,
                csd_dimension=config.csd_dimension,
                require_sfw=config.require_sfw,
                require_bluesky_accept=config.require_bluesky_accept,
            )
            if point_count <= 0:
                raise FaissIndexError("no policy-eligible current CLIP/CSD embeddings were found")

            use_flat = force_flat or point_count < config.flat_fallback_threshold
            actual_nlist = _actual_nlist(config, point_count)
            sample_size = 0
            clip_training: np.ndarray | None = None
            csd_training: np.ndarray | None = None
            if not use_flat:
                clip_training, csd_training = _training_sample(
                    connection,
                    app_config.models,
                    config,
                    point_count=point_count,
                )
                sample_size = int(clip_training.shape[0])

            clip_index = _new_index(
                faiss,
                dimension=config.clip_dimension,
                subquantizers=config.clip_pq_subquantizers,
                pq_bits=config.pq_bits,
                nlist=actual_nlist,
                training=clip_training,
                use_flat=use_flat,
            )
            csd_index = _new_index(
                faiss,
                dimension=config.csd_dimension,
                subquantizers=config.csd_pq_subquantizers,
                pq_bits=config.pq_bits,
                nlist=actual_nlist,
                training=csd_training,
                use_flat=use_flat,
            )
            _set_nprobe(faiss, clip_index, min(config.nprobe, actual_nlist))
            _set_nprobe(faiss, csd_index, min(config.nprobe, actual_nlist))

            clip_vectors = np.lib.format.open_memmap(
                temporary / _SNAPSHOT_FILES["clip_rerank"],
                mode="w+",
                dtype=np.float16,
                shape=(point_count, config.clip_dimension),
            )
            csd_vectors = np.lib.format.open_memmap(
                temporary / _SNAPSHOT_FILES["csd_rerank"],
                mode="w+",
                dtype=np.float16,
                shape=(point_count, config.csd_dimension),
            )
            catalog = _create_catalog(temporary / _SNAPSHOT_FILES["catalog"])
            try:
                _populate_snapshot(
                    connection,
                    catalog,
                    app_config.models,
                    config,
                    clip_index=clip_index,
                    csd_index=csd_index,
                    clip_vectors=clip_vectors,
                    csd_vectors=csd_vectors,
                )
            finally:
                catalog.close()
            clip_vectors.flush()
            csd_vectors.flush()
            del clip_vectors, csd_vectors

        faiss.write_index(clip_index, str(temporary / _SNAPSHOT_FILES["clip_index"]))
        faiss.write_index(csd_index, str(temporary / _SNAPSHOT_FILES["csd_index"]))
        file_evidence = {
            name: _file_evidence(temporary / filename)
            for name, filename in _SNAPSHOT_FILES.items()
        }
        index_kind = "flat" if use_flat else "opq_ivf_pq"
        model_bundle = faiss_model_bundle_version(app_config.models, config)
        manifest = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_version": version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "point_count": point_count,
            "model_bundle_version": model_bundle,
            "models": {
                "clip_name": app_config.models.clip_model_name,
                "clip_revision": app_config.models.clip_model_version,
                "csd_name": app_config.models.csd_model_name,
                "csd_revision": app_config.models.csd_model_version,
            },
            "dimensions": {
                "clip": config.clip_dimension,
                "csd": config.csd_dimension,
            },
            "index": {
                "kind": index_kind,
                "nlist": actual_nlist,
                "nprobe": min(config.nprobe, actual_nlist),
                "pq_bits": config.pq_bits,
                "clip_subquantizers": config.clip_pq_subquantizers,
                "csd_subquantizers": config.csd_pq_subquantizers,
                "training_sample_size": sample_size,
                "normalization": "l2-v1",
                "rerank_dtype": "float16",
                "fusion": "exact-rerank-rrf",
                "rrf_k": config.rrf_k,
            },
            "policy": {
                "require_sfw": config.require_sfw,
                "require_bluesky_accept": config.require_bluesky_accept,
            },
            "production_config_hash": production_config_hash,
            "faiss_version": getattr(faiss, "__version__", "unknown"),
            "files": file_evidence,
        }
        _atomic_json_write(temporary / "manifest.json", manifest)
        (temporary / "READY").write_text(f"{SNAPSHOT_SCHEMA_VERSION}\n", encoding="ascii")
        os.replace(temporary, destination)
        duration_ms = (time.perf_counter() - started) * 1000.0
        return FaissSnapshotBuildResult(
            snapshot_version=version,
            snapshot_path=str(destination.resolve()),
            point_count=point_count,
            index_kind=index_kind,
            nlist=actual_nlist,
            nprobe=min(config.nprobe, actual_nlist),
            training_sample_size=sample_size,
            byte_size=sum(int(item["byte_size"]) for item in file_evidence.values()),
            duration_ms=round(duration_ms, 3),
            model_bundle_version=model_bundle,
        )
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


class FaissSearchService:
    """Search compressed recall indexes, then rerank a bounded union exactly."""

    def __init__(
        self,
        config: FaissConfig,
        manifest: dict[str, Any],
        snapshot_path: Path,
    ) -> None:
        faiss = _faiss_import()
        self.config = config
        self.manifest = manifest
        self.snapshot_path = snapshot_path
        self.clip_index = faiss.read_index(str(snapshot_path / _SNAPSHOT_FILES["clip_index"]))
        self.csd_index = faiss.read_index(str(snapshot_path / _SNAPSHOT_FILES["csd_index"]))
        self.point_count = int(manifest["point_count"])
        self.clip_vectors = _load_rerank_matrix(
            snapshot_path / _SNAPSHOT_FILES["clip_rerank"],
            self.point_count,
            config.clip_dimension,
        )
        self.csd_vectors = _load_rerank_matrix(
            snapshot_path / _SNAPSHOT_FILES["csd_rerank"],
            self.point_count,
            config.csd_dimension,
        )
        self._catalog = sqlite3.connect(
            f"file:{(snapshot_path / _SNAPSHOT_FILES['catalog']).resolve()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self._catalog.row_factory = sqlite3.Row
        self._catalog_lock = threading.Lock()
        nprobe = int(manifest["index"]["nprobe"])
        _set_nprobe(faiss, self.clip_index, nprobe)
        _set_nprobe(faiss, self.csd_index, nprobe)

    @classmethod
    def from_active(cls, config: FaissConfig) -> "FaissSearchService":
        manifest, path = load_faiss_snapshot(config)
        return cls(config, manifest, path)

    @classmethod
    def from_snapshot(
        cls,
        config: FaissConfig,
        snapshot_version: str,
    ) -> "FaissSearchService":
        manifest, path = load_faiss_snapshot(config, snapshot_version=snapshot_version)
        return cls(config, manifest, path)

    @property
    def retrieval_contract(self) -> RetrievalContract:
        return RetrievalContract(
            backend="faiss",
            model_bundle_version=str(self.manifest["model_bundle_version"]),
            index_alias=self.config.alias_name,
            index_version=str(self.manifest["snapshot_version"]),
            candidate_limit=self.config.fusion_limit,
            clip_dimension=self.config.clip_dimension,
            csd_dimension=self.config.csd_dimension,
        )

    def close(self) -> None:
        self._catalog.close()

    def search_by_vectors(
        self,
        clip_vector: np.ndarray,
        style_vector: np.ndarray,
        *,
        top_k: int = 10,
        artist_id: str | None = None,
    ) -> list[SearchHit]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        clip = _validated_vector(clip_vector, self.config.clip_dimension, "CLIP")
        style = _validated_vector(style_vector, self.config.csd_dimension, "CSD")
        if artist_id is None:
            row_ids = self._ann_candidate_union(clip, style)
        else:
            row_ids = self._artist_row_ids(artist_id)
        hits = self._exact_fusion(row_ids, clip, style)
        max_per_artist = top_k if artist_id is not None else self.config.max_results_per_artist
        return _apply_artist_diversity(hits, top_k, max_per_artist)

    def search_by_artwork(
        self,
        artwork_id: str,
        *,
        top_k: int = 20,
        include_query_artist: bool = True,
    ) -> list[SearchHit]:
        record = self._record_for_artwork(artwork_id)
        if record is None:
            raise ValueError(f"artwork is not present in snapshot: {artwork_id!r}")
        row_id = int(record["row_id"])
        hits = self.search_by_vectors(
            np.asarray(self.clip_vectors[row_id], dtype=np.float32),
            np.asarray(self.csd_vectors[row_id], dtype=np.float32),
            top_k=max(top_k + 1, top_k * self.config.max_results_per_artist),
        )
        filtered = [
            hit
            for hit in hits
            if hit.artwork_id != artwork_id
            and (include_query_artist or hit.artist_id != str(record["artist_id"]))
        ]
        return [replace(hit, rank=index) for index, hit in enumerate(filtered[:top_k], start=1)]

    def search_by_clip_vector(
        self,
        clip_vector: np.ndarray,
        *,
        top_k: int = 20,
    ) -> list[SearchHit]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        clip = _validated_vector(clip_vector, self.config.clip_dimension, "CLIP")
        _, identifiers = self.clip_index.search(
            clip.reshape(1, -1),
            min(self.config.prefetch_limit, self.point_count),
        )
        row_ids = _valid_ids(identifiers[0])
        if not row_ids:
            return []
        scores = np.asarray(self.clip_vectors[row_ids], dtype=np.float32) @ clip
        order = np.argsort(-scores, kind="stable")
        records = self._records_for_rows(row_ids)
        hits = []
        for rank, offset in enumerate(order, start=1):
            row_id = row_ids[int(offset)]
            record = records.get(row_id)
            if record is None:
                continue
            score = float(scores[int(offset)])
            hits.append(
                SearchHit(
                    artwork_id=str(record["artwork_id"]),
                    artist_id=str(record["artist_id"]),
                    content_class=str(record["content_class"]),
                    score=score,
                    rank=rank,
                    clip_score=score,
                    clip_rank=rank,
                )
            )
        return _apply_artist_diversity(hits, top_k, self.config.max_results_per_artist)

    def _ann_candidate_union(self, clip: np.ndarray, style: np.ndarray) -> list[int]:
        limit = min(self.config.prefetch_limit, self.point_count)
        clip_scores, clip_ids = self.clip_index.search(clip.reshape(1, -1), limit)
        style_scores, style_ids = self.csd_index.search(style.reshape(1, -1), limit)
        clip_rank = {row_id: rank for rank, row_id in enumerate(_valid_ids(clip_ids[0]), 1)}
        style_rank = {row_id: rank for rank, row_id in enumerate(_valid_ids(style_ids[0]), 1)}
        candidates = set(clip_rank) | set(style_rank)
        missing_rank = limit + 1
        ordered = sorted(
            candidates,
            key=lambda row_id: -(
                1.0 / (self.config.rrf_k + clip_rank.get(row_id, missing_rank))
                + 1.0 / (self.config.rrf_k + style_rank.get(row_id, missing_rank))
            ),
        )
        return ordered[: self.config.rerank_limit]

    def _exact_fusion(
        self,
        row_ids: Sequence[int],
        clip: np.ndarray,
        style: np.ndarray,
    ) -> list[SearchHit]:
        if not row_ids:
            return []
        identifiers = np.asarray(row_ids, dtype=np.int64)
        clip_scores = np.asarray(self.clip_vectors[identifiers], dtype=np.float32) @ clip
        style_scores = np.asarray(self.csd_vectors[identifiers], dtype=np.float32) @ style
        clip_order = np.argsort(-clip_scores, kind="stable")
        style_order = np.argsort(-style_scores, kind="stable")
        clip_ranks = np.empty(len(identifiers), dtype=np.int64)
        style_ranks = np.empty(len(identifiers), dtype=np.int64)
        clip_ranks[clip_order] = np.arange(1, len(identifiers) + 1)
        style_ranks[style_order] = np.arange(1, len(identifiers) + 1)
        fusion = 1.0 / (self.config.rrf_k + clip_ranks) + 1.0 / (
            self.config.rrf_k + style_ranks
        )
        fusion_order = np.argsort(-fusion, kind="stable")[: self.config.fusion_limit]
        records = self._records_for_rows([int(value) for value in identifiers])
        hits: list[SearchHit] = []
        for fusion_rank, offset in enumerate(fusion_order, start=1):
            row_id = int(identifiers[int(offset)])
            record = records.get(row_id)
            if record is None:
                continue
            score = float(fusion[int(offset)])
            hits.append(
                SearchHit(
                    artwork_id=str(record["artwork_id"]),
                    artist_id=str(record["artist_id"]),
                    content_class=str(record["content_class"]),
                    score=score,
                    rank=fusion_rank,
                    fusion_score=score,
                    fusion_rank=fusion_rank,
                    clip_score=float(clip_scores[int(offset)]),
                    clip_rank=int(clip_ranks[int(offset)]),
                    style_score=float(style_scores[int(offset)]),
                    style_rank=int(style_ranks[int(offset)]),
                )
            )
        return hits

    def _artist_row_ids(self, artist_id: str) -> list[int]:
        with self._catalog_lock:
            rows = self._catalog.execute(
                "SELECT row_id FROM records WHERE artist_id = ? ORDER BY row_id",
                (artist_id,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def _record_for_artwork(self, artwork_id: str) -> sqlite3.Row | None:
        with self._catalog_lock:
            return self._catalog.execute(
                "SELECT * FROM records WHERE artwork_id = ?",
                (artwork_id,),
            ).fetchone()

    def _records_for_rows(self, row_ids: Sequence[int]) -> dict[int, sqlite3.Row]:
        unique_ids = sorted(set(row_ids))
        records: dict[int, sqlite3.Row] = {}
        with self._catalog_lock:
            for offset in range(0, len(unique_ids), 500):
                batch = unique_ids[offset : offset + 500]
                placeholders = ", ".join("?" for _ in batch)
                rows = self._catalog.execute(
                    f"SELECT * FROM records WHERE row_id IN ({placeholders})",
                    batch,
                ).fetchall()
                records.update({int(row["row_id"]): row for row in rows})
        return records


def evaluate_faiss_snapshot(
    service: FaissSearchService,
    *,
    sample_size: int = 20,
    top_k: int = 100,
    seed: int = 0,
) -> FaissEvaluationReport:
    if sample_size <= 0 or top_k <= 0:
        raise ValueError("evaluation sample size and top_k must be positive")
    if service.point_count < 2:
        raise FaissIndexError("at least two points are required for ANN evaluation")
    selected = random.Random(seed).sample(
        range(service.point_count),
        min(sample_size, service.point_count),
    )
    metrics = (
        _evaluate_vector(
            service.clip_index,
            service.clip_vectors,
            selected,
            vector_name="clip_subject",
            top_k=top_k,
        ),
        _evaluate_vector(
            service.csd_index,
            service.csd_vectors,
            selected,
            vector_name="csd_style",
            top_k=top_k,
        ),
    )
    search_latencies: list[float] = []
    for row_id in selected:
        started = time.perf_counter()
        service.search_by_vectors(
            np.asarray(service.clip_vectors[row_id], dtype=np.float32),
            np.asarray(service.csd_vectors[row_id], dtype=np.float32),
            top_k=min(top_k, service.config.fusion_limit),
        )
        search_latencies.append((time.perf_counter() - started) * 1000.0)
    quality_passed = (
        all(metric.mean_recall >= service.config.minimum_mean_recall for metric in metrics)
        and _percentile(search_latencies, 95) <= service.config.maximum_search_p95_ms
    )
    return FaissEvaluationReport(
        snapshot_version=str(service.manifest["snapshot_version"]),
        point_count=service.point_count,
        sampled_queries=len(selected),
        seed=seed,
        search_latency_p50_ms=_percentile(search_latencies, 50),
        search_latency_p95_ms=_percentile(search_latencies, 95),
        metrics=metrics,
        minimum_mean_recall_required=service.config.minimum_mean_recall,
        maximum_search_p95_ms_allowed=service.config.maximum_search_p95_ms,
        quality_gate_passed=quality_passed,
    )


def promote_faiss_snapshot(
    config: FaissConfig,
    snapshot_version: str,
    *,
    evaluation_path: str | Path,
    budget_path: str | Path,
) -> dict[str, object]:
    """Atomically activate a snapshot only after quality and cost gates pass."""

    manifest, snapshot_path = load_faiss_snapshot(
        config,
        snapshot_version=snapshot_version,
        verify_checksums=True,
    )
    evaluation = _read_json(evaluation_path)
    budget = _read_json(budget_path)
    if evaluation.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        raise FaissIndexError("evaluation report has an incompatible schema")
    if evaluation.get("snapshot_version") != snapshot_version:
        raise FaissIndexError("evaluation report targets a different snapshot")
    try:
        evaluation_points = int(evaluation.get("point_count", -1))
        metrics = evaluation.get("metrics")
        measured_recall = (
            min(float(metric.get("mean_recall", -1.0)) for metric in metrics)
            if isinstance(metrics, list) and metrics
            else -1.0
        )
        evaluation_p95 = float(evaluation.get("search_latency_p95_ms", float("inf")))
        evaluated_recall_gate = float(
            evaluation.get("minimum_mean_recall_required", -1.0)
        )
        evaluated_latency_gate = float(
            evaluation.get("maximum_search_p95_ms_allowed", float("inf"))
        )
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise FaissIndexError("evaluation report contains malformed numeric evidence") from exc
    if evaluation_points != int(manifest["point_count"]):
        raise FaissIndexError("evaluation report covers a different snapshot population")
    if (
        evaluation.get("quality_gate_passed") is not True
        or not all(
            math.isfinite(value)
            for value in (
                measured_recall,
                evaluation_p95,
                evaluated_recall_gate,
                evaluated_latency_gate,
            )
        )
        or measured_recall < config.minimum_mean_recall
        or evaluation_p95 > config.maximum_search_p95_ms
        or evaluated_recall_gate < config.minimum_mean_recall
        or evaluated_latency_gate > config.maximum_search_p95_ms
    ):
        raise FaissIndexError("snapshot did not pass the configured retrieval quality gate")
    if budget.get("schema_version") != CAPACITY_PLAN_VERSION:
        raise FaissIndexError("capacity plan has an incompatible schema")
    try:
        budget_points = int(budget.get("target_points", -1))
        monthly_budget = float(budget.get("monthly_budget_usd", 0.0))
        projected_monthly = float(budget.get("projected_monthly_usd", float("inf")))
        required_headroom = float(budget.get("required_headroom_usd", float("inf")))
    except (TypeError, ValueError, OverflowError) as exc:
        raise FaissIndexError("capacity plan contains malformed numeric evidence") from exc
    if budget_points < int(manifest["point_count"]):
        raise FaissIndexError("capacity plan does not cover the snapshot population")
    if (
        budget.get("promotion_ready") is not True
        or not all(
            math.isfinite(value)
            for value in (monthly_budget, projected_monthly, required_headroom)
        )
        or monthly_budget - projected_monthly < required_headroom
    ):
        raise FaissIndexError("capacity plan did not pass the configured monthly budget gate")
    pointer = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_version": snapshot_version,
        "manifest_sha256": _sha256(snapshot_path / "manifest.json"),
        "model_bundle_version": manifest["model_bundle_version"],
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    }
    config.artifact_root.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(config.artifact_root / config.active_pointer, pointer)
    return pointer


def load_faiss_snapshot(
    config: FaissConfig,
    *,
    snapshot_version: str | None = None,
    verify_checksums: bool | None = None,
) -> tuple[dict[str, Any], Path]:
    if snapshot_version is None:
        pointer_path = config.artifact_root / config.active_pointer
        if not pointer_path.is_file():
            raise FaissIndexError(f"active FAISS pointer does not exist: {pointer_path}")
        pointer = _read_json(pointer_path)
        snapshot_version = str(pointer.get("snapshot_version", ""))
        expected_manifest_sha256 = pointer.get("manifest_sha256")
    else:
        expected_manifest_sha256 = None
    version = _safe_snapshot_version(snapshot_version)
    snapshot_path = config.artifact_root / "snapshots" / version
    manifest_path = snapshot_path / "manifest.json"
    if not (snapshot_path / "READY").is_file() or not manifest_path.is_file():
        raise FaissIndexError(f"FAISS snapshot is incomplete: {snapshot_path}")
    manifest = _read_json(manifest_path)
    if expected_manifest_sha256 is not None and _sha256(manifest_path) != expected_manifest_sha256:
        raise FaissIndexError("active FAISS pointer manifest checksum failed")
    if manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise FaissIndexError("FAISS snapshot schema is incompatible")
    if manifest.get("snapshot_version") != version:
        raise FaissIndexError("FAISS snapshot directory and manifest versions differ")
    dimensions = manifest.get("dimensions", {})
    if dimensions != {"clip": config.clip_dimension, "csd": config.csd_dimension}:
        raise FaissIndexError("FAISS snapshot dimensions do not match configuration")
    full_verify = config.verify_checksums_on_load if verify_checksums is None else verify_checksums
    for name, filename in _SNAPSHOT_FILES.items():
        path = snapshot_path / filename
        evidence = manifest.get("files", {}).get(name, {})
        if not path.is_file() or path.stat().st_size != int(evidence.get("byte_size", -1)):
            raise FaissIndexError(f"FAISS snapshot file is missing or truncated: {filename}")
        if full_verify and _sha256(path) != evidence.get("sha256"):
            raise FaissIndexError(f"FAISS snapshot checksum failed: {filename}")
    return manifest, snapshot_path


def faiss_snapshot_status(config: FaissConfig) -> dict[str, object]:
    try:
        manifest, path = load_faiss_snapshot(config)
    except FaissIndexError as exc:
        return {
            "enabled": config.enabled,
            "ready": False,
            "alias_name": config.alias_name,
            "error": str(exc),
        }
    return {
        "enabled": config.enabled,
        "ready": True,
        "alias_name": config.alias_name,
        "snapshot_version": manifest["snapshot_version"],
        "point_count": manifest["point_count"],
        "index_kind": manifest["index"]["kind"],
        "model_bundle_version": manifest["model_bundle_version"],
        "path": str(path.resolve()),
    }


def _training_sample(
    connection: sqlite3.Connection,
    models: ModelConfig,
    config: FaissConfig,
    *,
    point_count: int,
) -> tuple[np.ndarray, np.ndarray]:
    target = min(config.training_sample_size, point_count)
    stride = max(point_count // target, 1)
    clip: list[np.ndarray] = []
    csd: list[np.ndarray] = []
    cursor = eligible_embedding_rows(
        connection,
        models,
        clip_dimension=config.clip_dimension,
        csd_dimension=config.csd_dimension,
        require_sfw=config.require_sfw,
        require_bluesky_accept=config.require_bluesky_accept,
    )
    for index, row in enumerate(cursor):
        if index % stride or len(clip) >= target:
            continue
        clip.append(_row_vector(row, "clip_vector", "clip_dim", config.clip_dimension))
        csd.append(_row_vector(row, "csd_style", "csd_style_dim", config.csd_dimension))
    return np.ascontiguousarray(clip, dtype=np.float32), np.ascontiguousarray(
        csd, dtype=np.float32
    )


def _new_index(
    faiss: Any,
    *,
    dimension: int,
    subquantizers: int,
    pq_bits: int,
    nlist: int,
    training: np.ndarray | None,
    use_flat: bool,
) -> Any:
    if use_flat:
        return faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
    if training is None or not len(training):
        raise FaissIndexError("an IVF/PQ index requires training vectors")
    quantizer = faiss.IndexFlatIP(dimension)
    ivfpq = faiss.IndexIVFPQ(
        quantizer,
        dimension,
        nlist,
        subquantizers,
        pq_bits,
        faiss.METRIC_INNER_PRODUCT,
    )
    opq = faiss.OPQMatrix(dimension, subquantizers)
    index = faiss.IndexPreTransform(opq, ivfpq)
    index.train(np.ascontiguousarray(training, dtype=np.float32))
    if not index.is_trained:
        raise FaissIndexError("FAISS OPQ/IVF/PQ training did not complete")
    return index


def _populate_snapshot(
    connection: sqlite3.Connection,
    catalog: sqlite3.Connection,
    models: ModelConfig,
    config: FaissConfig,
    *,
    clip_index: Any,
    csd_index: Any,
    clip_vectors: np.memmap,
    csd_vectors: np.memmap,
) -> None:
    cursor = eligible_embedding_rows(
        connection,
        models,
        clip_dimension=config.clip_dimension,
        csd_dimension=config.csd_dimension,
        require_sfw=config.require_sfw,
        require_bluesky_accept=config.require_bluesky_accept,
    )
    row_id = 0
    while rows := cursor.fetchmany(config.add_batch_size):
        clip_batch = np.stack(
            [_row_vector(row, "clip_vector", "clip_dim", config.clip_dimension) for row in rows]
        )
        csd_batch = np.stack(
            [
                _row_vector(row, "csd_style", "csd_style_dim", config.csd_dimension)
                for row in rows
            ]
        )
        identifiers = np.arange(row_id, row_id + len(rows), dtype=np.int64)
        clip_index.add_with_ids(np.ascontiguousarray(clip_batch), identifiers)
        csd_index.add_with_ids(np.ascontiguousarray(csd_batch), identifiers)
        clip_vectors[row_id : row_id + len(rows)] = clip_batch.astype(np.float16)
        csd_vectors[row_id : row_id + len(rows)] = csd_batch.astype(np.float16)
        catalog.executemany(
            """
            INSERT INTO records (
                row_id, artwork_id, artist_id, artist_display_name, content_class,
                source_platform, source_id, author_did, post_uri, image_index,
                object_key, object_uri, object_role, created_at, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row_id + offset,
                    str(row["artwork_id"]),
                    str(row["artist_id"]),
                    str(row["artist_display_name"]),
                    str(row["content_class"] or "unknown"),
                    str(row["source_platform"] or "unknown"),
                    row["source_id"],
                    row["author_did"],
                    row["post_uri"],
                    row["image_index"],
                    row["object_key"],
                    row["object_uri"],
                    row["object_role"],
                    row["date_added"],
                    _row_content_hash(row),
                )
                for offset, row in enumerate(rows)
            ],
        )
        catalog.commit()
        row_id += len(rows)
    if row_id != clip_vectors.shape[0]:
        raise FaissIndexError(
            f"eligible corpus changed during build: expected {clip_vectors.shape[0]}, read {row_id}"
        )


def _create_catalog(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = FULL;
        CREATE TABLE records (
            row_id INTEGER PRIMARY KEY,
            artwork_id TEXT NOT NULL UNIQUE,
            artist_id TEXT NOT NULL,
            artist_display_name TEXT NOT NULL,
            content_class TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            source_id TEXT,
            author_did TEXT,
            post_uri TEXT,
            image_index INTEGER,
            object_key TEXT,
            object_uri TEXT,
            object_role TEXT,
            created_at TEXT,
            content_hash TEXT NOT NULL
        );
        CREATE INDEX records_artist ON records(artist_id, row_id);
        CREATE INDEX records_source ON records(source_platform, source_id);
        """
    )
    return connection


def _evaluate_vector(
    index: Any,
    matrix: np.ndarray,
    selected: Sequence[int],
    *,
    vector_name: str,
    top_k: int,
) -> FaissVectorMetrics:
    recalls: list[float] = []
    approximate_latencies: list[float] = []
    exact_latencies: list[float] = []
    effective_k = min(top_k, max(int(matrix.shape[0]) - 1, 1))
    for row_id in selected:
        query = np.asarray(matrix[row_id], dtype=np.float32)
        started = time.perf_counter()
        _, identifiers = index.search(query.reshape(1, -1), min(effective_k + 1, len(matrix)))
        approximate_latencies.append((time.perf_counter() - started) * 1000.0)
        approximate = [value for value in _valid_ids(identifiers[0]) if value != row_id][
            :effective_k
        ]
        started = time.perf_counter()
        exact = _exact_top_ids(matrix, query, effective_k, exclude_row_id=row_id)
        exact_latencies.append((time.perf_counter() - started) * 1000.0)
        recalls.append(len(set(approximate) & set(exact)) / len(exact) if exact else 1.0)
    return FaissVectorMetrics(
        vector_name=vector_name,
        query_count=len(selected),
        top_k=effective_k,
        mean_recall=float(np.mean(recalls)),
        minimum_recall=float(np.min(recalls)),
        approximate_latency_p50_ms=_percentile(approximate_latencies, 50),
        approximate_latency_p95_ms=_percentile(approximate_latencies, 95),
        exact_latency_p50_ms=_percentile(exact_latencies, 50),
        exact_latency_p95_ms=_percentile(exact_latencies, 95),
    )


def _exact_top_ids(
    matrix: np.ndarray,
    query: np.ndarray,
    top_k: int,
    *,
    exclude_row_id: int | None = None,
    chunk_size: int = 100_000,
) -> list[int]:
    best_ids = np.empty(0, dtype=np.int64)
    best_scores = np.empty(0, dtype=np.float32)
    for offset in range(0, len(matrix), chunk_size):
        stop = min(offset + chunk_size, len(matrix))
        scores = np.asarray(matrix[offset:stop], dtype=np.float32) @ query
        ids = np.arange(offset, stop, dtype=np.int64)
        if exclude_row_id is not None and offset <= exclude_row_id < stop:
            scores[exclude_row_id - offset] = -np.inf
        merged_scores = np.concatenate((best_scores, scores))
        merged_ids = np.concatenate((best_ids, ids))
        keep = min(top_k, len(merged_scores))
        if keep < len(merged_scores):
            selected = np.argpartition(merged_scores, -keep)[-keep:]
            best_scores = merged_scores[selected]
            best_ids = merged_ids[selected]
        else:
            best_scores = merged_scores
            best_ids = merged_ids
    return [int(best_ids[index]) for index in np.argsort(-best_scores, kind="stable")]


def _actual_nlist(config: FaissConfig, point_count: int) -> int:
    maximum = max(1, min(config.nlist, point_count // 39))
    return 1 << (maximum.bit_length() - 1)


def _set_nprobe(faiss: Any, index: Any, nprobe: int) -> None:
    try:
        faiss.extract_index_ivf(index).nprobe = nprobe
    except RuntimeError:
        return


def _row_vector(
    row: sqlite3.Row,
    blob_column: str,
    dimension_column: str,
    expected_dimension: int,
) -> np.ndarray:
    dimension = int(row[dimension_column])
    if dimension != expected_dimension:
        raise FaissIndexError(
            f"{blob_column} dimension is {dimension}, expected {expected_dimension}"
        )
    vector = l2_normalize(blob_to_vector(bytes(row[blob_column]), dimension))
    if not np.isfinite(vector).all() or not np.any(vector):
        raise FaissIndexError(f"{blob_column} contains a non-finite or zero vector")
    return np.ascontiguousarray(vector, dtype=np.float32)


def _validated_vector(values: np.ndarray, dimension: int, name: str) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32).reshape(-1)
    if vector.shape != (dimension,):
        raise ValueError(f"{name} query vector must have dimension {dimension}")
    if not np.isfinite(vector).all() or not np.any(vector):
        raise ValueError(f"{name} query vector must be finite and non-zero")
    return np.ascontiguousarray(l2_normalize(vector), dtype=np.float32)


def _row_content_hash(row: sqlite3.Row) -> str:
    digest = hashlib.sha256()
    digest.update(bytes(row["clip_vector"]))
    digest.update(bytes(row["csd_style"]))
    digest.update(str(row["artwork_id"]).encode("utf-8"))
    digest.update(str(row["content_class"] or "unknown").encode("utf-8"))
    return digest.hexdigest()


def _load_rerank_matrix(path: Path, rows: int, dimension: int) -> np.ndarray:
    matrix = np.load(path, mmap_mode="r", allow_pickle=False)
    if matrix.dtype != np.float16 or matrix.shape != (rows, dimension):
        raise FaissIndexError(f"rerank matrix has an invalid dtype or shape: {path.name}")
    return matrix


def _apply_artist_diversity(
    hits: Sequence[SearchHit],
    top_k: int,
    max_per_artist: int,
) -> list[SearchHit]:
    counts: dict[str, int] = {}
    selected: list[SearchHit] = []
    for hit in hits:
        count = counts.get(hit.artist_id, 0)
        if count >= max_per_artist:
            continue
        counts[hit.artist_id] = count + 1
        selected.append(replace(hit, rank=len(selected) + 1))
        if len(selected) >= top_k:
            break
    return selected


def _valid_ids(values: np.ndarray) -> list[int]:
    return [int(value) for value in values if int(value) >= 0]


def _safe_snapshot_version(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized):
        raise FaissIndexError("snapshot version must be a safe 1-128 character identifier")
    return normalized


def _file_evidence(path: Path) -> dict[str, object]:
    return {
        "filename": path.name,
        "byte_size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FaissIndexError(f"could not read JSON evidence {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FaissIndexError(f"JSON evidence must be an object: {path}")
    return payload


def _atomic_json_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    return round(float(np.percentile(np.asarray(values, dtype=np.float64), percentile)), 3)


def _faiss_import() -> Any:
    try:
        import faiss
    except ImportError as exc:
        raise FaissDependencyError(
            "FAISS support requires the scale extra: pip install -e '.[scale]'"
        ) from exc
    return faiss
