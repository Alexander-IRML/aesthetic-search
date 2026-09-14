from __future__ import annotations

from pathlib import Path
import hashlib
import json
import tomllib
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ObjectStoreConfig(BaseModel):
    provider: Literal["local", "s3"] = "local"
    local_root: Path = Path("data/object_store")
    bucket: str = ""
    prefix: str = "artsearch"
    endpoint_url: str = ""
    region: str = ""
    upload_concurrency: int = 8
    upload_checkpoint_batch_size: int = 256
    upload_max_attempts: int = 4
    upload_retry_base_seconds: float = 0.5
    publish_corpus_renditions: bool = True
    corpus_source: Literal["raw", "processed"] = "raw"
    corpus_role: Literal["original", "display"] = "original"
    corpus_namespace: str = "corpus/originals"

    @field_validator("prefix", "corpus_namespace")
    @classmethod
    def _normalize_prefix(cls, value: str) -> str:
        normalized = value.strip("/")
        if not normalized:
            raise ValueError("object-store prefixes must not be empty")
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("object-store prefixes must be safe object-key prefixes")
        return normalized

    @model_validator(mode="after")
    def _validate_provider_settings(self) -> "ObjectStoreConfig":
        if self.provider == "s3" and not self.bucket.strip():
            raise ValueError("object_store.bucket is required for the s3 provider")
        if self.endpoint_url and not self.endpoint_url.startswith("https://"):
            raise ValueError("object_store.endpoint_url must use HTTPS")
        if self.upload_concurrency <= 0:
            raise ValueError("object_store.upload_concurrency must be positive")
        if self.upload_checkpoint_batch_size <= 0:
            raise ValueError("object_store.upload_checkpoint_batch_size must be positive")
        if self.upload_max_attempts <= 0:
            raise ValueError("object_store.upload_max_attempts must be positive")
        if self.upload_retry_base_seconds < 0:
            raise ValueError("object_store.upload_retry_base_seconds must be non-negative")
        expected_role = "original" if self.corpus_source == "raw" else "display"
        if self.corpus_role != expected_role:
            raise ValueError(
                "object_store.corpus_role must be 'original' for raw sources or "
                "'display' for processed sources"
            )
        return self


class ManifestConfig(BaseModel):
    local_dir: Path = Path("data/production/manifests")
    object_prefix: str = "manifests"

    @field_validator("object_prefix")
    @classmethod
    def _safe_object_prefix(cls, value: str) -> str:
        normalized = value.strip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("manifests.object_prefix must be a safe object-key prefix")
        return normalized


class QdrantConfig(BaseModel):
    enabled: bool = False
    url: str = "http://localhost:6333"
    url_env: str = "QDRANT_URL"
    api_key_env: str = "QDRANT_API_KEY"
    require_api_key: bool = False
    collection_name: str = "artworks_clip3_csd1_v2"
    alias_name: str = "artworks_active"
    clip_vector_name: str = "clip_subject"
    csd_vector_name: str = "csd_style"
    clip_dimension: int = 512
    csd_dimension: int = 768
    datatype: Literal["float16", "float32"] = "float16"
    on_disk_vectors: bool = False
    on_disk_payload: bool = False
    batch_size: int = 256
    timeout_seconds: int = 30
    prefer_grpc: bool = False
    hnsw_ef: int = 128
    prefetch_limit: int = 200
    fusion_limit: int = 100
    max_results_per_artist: int = 2
    require_sfw: bool = True
    require_bluesky_accept: bool = True
    promote_after_sync: bool = True

    @field_validator(
        "url_env",
        "api_key_env",
        "collection_name",
        "alias_name",
        "clip_vector_name",
        "csd_vector_name",
    )
    @classmethod
    def _nonempty_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Qdrant names and environment-variable names must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_limits(self) -> "QdrantConfig":
        if self.collection_name == self.alias_name:
            raise ValueError("qdrant.collection_name and alias_name must differ")
        if self.clip_vector_name == self.csd_vector_name:
            raise ValueError("Qdrant named vectors must have distinct names")
        positive = {
            "clip_dimension": self.clip_dimension,
            "csd_dimension": self.csd_dimension,
            "batch_size": self.batch_size,
            "timeout_seconds": self.timeout_seconds,
            "hnsw_ef": self.hnsw_ef,
            "prefetch_limit": self.prefetch_limit,
            "fusion_limit": self.fusion_limit,
            "max_results_per_artist": self.max_results_per_artist,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"qdrant.{name} must be positive")
        if self.fusion_limit > self.prefetch_limit:
            raise ValueError("qdrant.fusion_limit must not exceed prefetch_limit")
        return self


class FaissConfig(BaseModel):
    """Immutable FAISS snapshot and exact-rerank serving contract."""

    enabled: bool = False
    artifact_root: Path = Path("data/indexes/faiss")
    active_pointer: str = "active.json"
    alias_name: str = "artworks_faiss_active"
    clip_dimension: int = 512
    csd_dimension: int = 768
    clip_pq_subquantizers: int = 32
    csd_pq_subquantizers: int = 48
    pq_bits: int = 8
    nlist: int = 4096
    nprobe: int = 32
    flat_fallback_threshold: int = 50_000
    training_sample_size: int = 250_000
    add_batch_size: int = 4096
    prefetch_limit: int = 500
    rerank_limit: int = 500
    fusion_limit: int = 100
    max_results_per_artist: int = 2
    rrf_k: int = 60
    require_sfw: bool = True
    require_bluesky_accept: bool = True
    minimum_mean_recall: float = 0.90
    maximum_search_p95_ms: float = 250.0
    verify_checksums_on_load: bool = False

    @field_validator("active_pointer", "alias_name")
    @classmethod
    def _safe_faiss_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or Path(normalized).name != normalized:
            raise ValueError("FAISS pointer and alias names must be non-empty file-safe names")
        return normalized

    @model_validator(mode="after")
    def _validate_faiss_limits(self) -> "FaissConfig":
        positive = {
            "clip_dimension": self.clip_dimension,
            "csd_dimension": self.csd_dimension,
            "clip_pq_subquantizers": self.clip_pq_subquantizers,
            "csd_pq_subquantizers": self.csd_pq_subquantizers,
            "pq_bits": self.pq_bits,
            "nlist": self.nlist,
            "nprobe": self.nprobe,
            "flat_fallback_threshold": self.flat_fallback_threshold,
            "training_sample_size": self.training_sample_size,
            "add_batch_size": self.add_batch_size,
            "prefetch_limit": self.prefetch_limit,
            "rerank_limit": self.rerank_limit,
            "fusion_limit": self.fusion_limit,
            "max_results_per_artist": self.max_results_per_artist,
            "rrf_k": self.rrf_k,
            "maximum_search_p95_ms": self.maximum_search_p95_ms,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"faiss.{name} must be positive")
        if self.clip_dimension % self.clip_pq_subquantizers:
            raise ValueError("faiss.clip_dimension must be divisible by its PQ subquantizers")
        if self.csd_dimension % self.csd_pq_subquantizers:
            raise ValueError("faiss.csd_dimension must be divisible by its PQ subquantizers")
        if self.pq_bits > 16:
            raise ValueError("faiss.pq_bits must be at most 16")
        if self.nprobe > self.nlist:
            raise ValueError("faiss.nprobe must not exceed nlist")
        if not self.fusion_limit <= self.rerank_limit <= self.prefetch_limit * 2:
            raise ValueError(
                "faiss limits must satisfy fusion_limit <= rerank_limit <= 2 * prefetch_limit"
            )
        if not 0.0 <= self.minimum_mean_recall <= 1.0:
            raise ValueError("faiss.minimum_mean_recall must be in [0, 1]")
        return self


class ScaleBudgetConfig(BaseModel):
    """Editable assumptions used by the pre-promotion monthly cost gate."""

    target_points: int = 10_000_000
    monthly_budget_usd: float = 50.0
    minimum_headroom_usd: float = 5.0
    vector_host_monthly_usd: float = 24.0
    edge_compute_monthly_usd: float = 5.0
    metadata_database_monthly_usd: float = 5.0
    observability_monthly_usd: float = 0.0
    contingency_monthly_usd: float = 5.0
    object_storage_gb_monthly_usd: float = 0.015
    hot_image_cache_fraction: float = 0.01
    average_cached_image_bytes: int = 62_500
    metadata_bytes_per_point: int = 320
    retained_snapshot_copies: int = 2
    store_exact_fp16_rerank_vectors: bool = True

    @model_validator(mode="after")
    def _validate_budget(self) -> "ScaleBudgetConfig":
        positive = {
            "target_points": self.target_points,
            "monthly_budget_usd": self.monthly_budget_usd,
            "average_cached_image_bytes": self.average_cached_image_bytes,
            "metadata_bytes_per_point": self.metadata_bytes_per_point,
            "retained_snapshot_copies": self.retained_snapshot_copies,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"scale_budget.{name} must be positive")
        non_negative = {
            "minimum_headroom_usd": self.minimum_headroom_usd,
            "vector_host_monthly_usd": self.vector_host_monthly_usd,
            "edge_compute_monthly_usd": self.edge_compute_monthly_usd,
            "metadata_database_monthly_usd": self.metadata_database_monthly_usd,
            "observability_monthly_usd": self.observability_monthly_usd,
            "contingency_monthly_usd": self.contingency_monthly_usd,
            "object_storage_gb_monthly_usd": self.object_storage_gb_monthly_usd,
        }
        for name, value in non_negative.items():
            if value < 0:
                raise ValueError(f"scale_budget.{name} must be non-negative")
        if not 0.0 <= self.hot_image_cache_fraction <= 1.0:
            raise ValueError("scale_budget.hot_image_cache_fraction must be in [0, 1]")
        if self.minimum_headroom_usd >= self.monthly_budget_usd:
            raise ValueError("scale_budget.minimum_headroom_usd must be below the budget")
        if not self.store_exact_fp16_rerank_vectors:
            raise ValueError(
                "scale_budget must account for the exact FP16 rerank vectors used by FAISS"
            )
        return self


class LocalRetentionConfig(BaseModel):
    """Opt-in deletion policy for transient accepted-image working files."""

    enabled: bool = False
    delete_raw: bool = True
    delete_processed: bool = True
    require_source_reference: bool = True

    @model_validator(mode="after")
    def _validate_retention(self) -> "LocalRetentionConfig":
        if self.enabled and not (self.delete_raw or self.delete_processed):
            raise ValueError("local_retention must delete at least one rendition when enabled")
        return self


class PostgresConfig(BaseModel):
    enabled: bool = False
    require_tls: bool = False
    database_url_env: str = "DATABASE_URL"
    direct_url_env: str = "DATABASE_DIRECT_URL"
    application_name: str = "artsearch"
    pool_size: int = 5
    max_overflow: int = 5
    pool_timeout_seconds: int = 10
    statement_timeout_ms: int = 15_000
    telemetry_retention_days: int = 180
    outbox_batch_size: int = 100
    publish_embeddings: bool = True
    publish_filter_history: bool = True
    publish_run_history: bool = True

    @field_validator("database_url_env", "direct_url_env", "application_name")
    @classmethod
    def _nonempty_postgres_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("PostgreSQL environment and application names must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_postgres_limits(self) -> "PostgresConfig":
        positive = {
            "pool_size": self.pool_size,
            "pool_timeout_seconds": self.pool_timeout_seconds,
            "statement_timeout_ms": self.statement_timeout_ms,
            "telemetry_retention_days": self.telemetry_retention_days,
            "outbox_batch_size": self.outbox_batch_size,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"postgres.{name} must be positive")
        if self.max_overflow < 0:
            raise ValueError("postgres.max_overflow must be non-negative")
        if self.database_url_env == self.direct_url_env:
            raise ValueError("pooled and direct PostgreSQL URL environment names must differ")
        return self


class ProductionConfig(BaseModel):
    version: str = "1.0.0"
    object_store: ObjectStoreConfig = Field(default_factory=ObjectStoreConfig)
    manifests: ManifestConfig = Field(default_factory=ManifestConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    faiss: FaissConfig = Field(default_factory=FaissConfig)
    scale_budget: ScaleBudgetConfig = Field(default_factory=ScaleBudgetConfig)
    local_retention: LocalRetentionConfig = Field(default_factory=LocalRetentionConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    config_hash: str = ""

    @field_validator("version")
    @classmethod
    def _version_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("production config version is required")
        return value


def load_production_config(
    path: str | Path = "configs/production.default.toml",
) -> ProductionConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    config = ProductionConfig.model_validate(raw)
    payload = config.model_dump(mode="json", exclude={"config_hash"})
    config.config_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    root_dir = config_path.resolve().parents[1]
    if not config.object_store.local_root.is_absolute():
        config.object_store.local_root = root_dir / config.object_store.local_root
    if not config.manifests.local_dir.is_absolute():
        config.manifests.local_dir = root_dir / config.manifests.local_dir
    if not config.faiss.artifact_root.is_absolute():
        config.faiss.artifact_root = root_dir / config.faiss.artifact_root
    return config
