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

    @field_validator("prefix")
    @classmethod
    def _normalize_prefix(cls, value: str) -> str:
        normalized = value.strip("/")
        if not normalized:
            raise ValueError("object_store.prefix must not be empty")
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("object_store.prefix must be a safe object-key prefix")
        return normalized

    @model_validator(mode="after")
    def _validate_provider_settings(self) -> "ObjectStoreConfig":
        if self.provider == "s3" and not self.bucket.strip():
            raise ValueError("object_store.bucket is required for the s3 provider")
        if self.endpoint_url and not self.endpoint_url.startswith("https://"):
            raise ValueError("object_store.endpoint_url must use HTTPS")
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
    collection_name: str = "artworks_clip3_dino1_v1"
    alias_name: str = "artworks_active"
    clip_vector_name: str = "clip_subject"
    dino_vector_name: str = "dino_global"
    clip_dimension: int = 512
    dino_dimension: int = 768
    datatype: Literal["float16", "float32"] = "float16"
    on_disk_vectors: bool = False
    on_disk_payload: bool = False
    batch_size: int = 256
    timeout_seconds: int = 30
    prefer_grpc: bool = False
    hnsw_ef: int = 128
    prefetch_limit: int = 200
    fusion_limit: int = 100
    patch_rerank_limit: int = 50
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
        "dino_vector_name",
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
        if self.clip_vector_name == self.dino_vector_name:
            raise ValueError("Qdrant named vectors must have distinct names")
        positive = {
            "clip_dimension": self.clip_dimension,
            "dino_dimension": self.dino_dimension,
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
        if self.patch_rerank_limit < 0:
            raise ValueError("qdrant.patch_rerank_limit must be non-negative")
        if self.fusion_limit > self.prefetch_limit:
            raise ValueError("qdrant.fusion_limit must not exceed prefetch_limit")
        if self.patch_rerank_limit > self.fusion_limit:
            raise ValueError("qdrant.patch_rerank_limit must not exceed fusion_limit")
        return self


class ProductionConfig(BaseModel):
    version: str = "1.0.0"
    object_store: ObjectStoreConfig = Field(default_factory=ObjectStoreConfig)
    manifests: ManifestConfig = Field(default_factory=ManifestConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
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
    return config
