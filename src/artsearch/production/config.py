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


class ProductionConfig(BaseModel):
    version: str = "1.0.0"
    object_store: ObjectStoreConfig = Field(default_factory=ObjectStoreConfig)
    manifests: ManifestConfig = Field(default_factory=ManifestConfig)
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
