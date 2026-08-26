from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tomllib
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class SearchApiConfig(BaseModel):
    version: str = "1.0.0"
    host: str = "127.0.0.1"
    port: int = 8000
    catalog_backend: Literal["sqlite", "postgres"] = "sqlite"
    telemetry_enabled: bool = True
    serve_local_images: bool = True
    asset_base_url: str = ""
    asset_base_url_env: str = "ARTSEARCH_ASSET_BASE_URL"
    result_limit: int = 10
    candidate_telemetry_limit: int = 100
    max_upload_bytes: int = 10_000_000
    max_image_pixels: int = 40_000_000
    allowed_mime_types: tuple[str, ...] = (
        "image/jpeg",
        "image/png",
        "image/webp",
    )
    max_concurrent_inferences: int = 1
    inference_queue_timeout_seconds: float = 2.0
    inference_timeout_seconds: float = 120.0
    rate_limit_requests: int = 12
    rate_limit_window_seconds: int = 60
    feedback_rate_limit_requests: int = 120
    feedback_rate_limit_window_seconds: int = 60
    trust_proxy_headers: bool = False
    config_hash: str = Field(default="", exclude=True)

    @field_validator("version", "host", "asset_base_url_env")
    @classmethod
    def _required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("API version and host must not be empty")
        return normalized

    @field_validator("asset_base_url")
    @classmethod
    def _normalize_asset_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            return normalized
        if normalized.startswith("/"):
            parsed = urlsplit(normalized)
            if normalized.startswith("//") or parsed.query or parsed.fragment:
                raise ValueError("relative asset_base_url must be an absolute path")
            return normalized
        if not normalized.startswith("https://"):
            raise ValueError("asset_base_url must use HTTPS or an absolute path")
        return normalized

    @field_validator("allowed_mime_types")
    @classmethod
    def _validate_mime_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(value.strip().lower() for value in values))
        supported = {"image/jpeg", "image/png", "image/webp"}
        if not normalized or not set(normalized) <= supported:
            raise ValueError(f"allowed_mime_types must be a non-empty subset of {supported}")
        return normalized

    @model_validator(mode="after")
    def _validate_limits(self) -> "SearchApiConfig":
        positive = {
            "port": self.port,
            "result_limit": self.result_limit,
            "candidate_telemetry_limit": self.candidate_telemetry_limit,
            "max_upload_bytes": self.max_upload_bytes,
            "max_image_pixels": self.max_image_pixels,
            "max_concurrent_inferences": self.max_concurrent_inferences,
            "inference_queue_timeout_seconds": self.inference_queue_timeout_seconds,
            "inference_timeout_seconds": self.inference_timeout_seconds,
            "rate_limit_requests": self.rate_limit_requests,
            "rate_limit_window_seconds": self.rate_limit_window_seconds,
            "feedback_rate_limit_requests": self.feedback_rate_limit_requests,
            "feedback_rate_limit_window_seconds": self.feedback_rate_limit_window_seconds,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.port > 65_535:
            raise ValueError("port must be at most 65535")
        if self.result_limit > 50:
            raise ValueError("result_limit must be at most 50")
        if self.candidate_telemetry_limit < self.result_limit:
            raise ValueError("candidate_telemetry_limit must cover every displayed result")
        if self.candidate_telemetry_limit > 500:
            raise ValueError("candidate_telemetry_limit must be at most 500")
        if self.catalog_backend == "postgres" and self.serve_local_images:
            raise ValueError("serve_local_images must be false when catalog_backend is postgres")
        if self.catalog_backend == "postgres" and not self.asset_base_url:
            raise ValueError("asset_base_url is required for the postgres catalog")
        return self


def load_search_api_config(
    path: str | Path = "configs/search_api.default.toml",
) -> SearchApiConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    asset_base_url_env = str(raw.get("asset_base_url_env", "ARTSEARCH_ASSET_BASE_URL")).strip()
    if asset_base_url_env:
        environment_value = os.environ.get(asset_base_url_env, "").strip()
        if environment_value:
            raw["asset_base_url"] = environment_value
    config = SearchApiConfig.model_validate(raw)
    payload = config.model_dump(mode="json", exclude={"config_hash"})
    config.config_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return config
