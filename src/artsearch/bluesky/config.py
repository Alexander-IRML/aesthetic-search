from __future__ import annotations

from pathlib import Path
import tomllib

from pydantic import BaseModel, Field, field_validator, model_validator

from artsearch.artwork_filter.hashing import stable_json_hash


class BlueskyAPIConfig(BaseModel):
    base_url: str = "https://public.api.bsky.app"
    timeout_seconds: float = 20.0
    user_agent: str = "ArtSearchResearch/0.1"
    page_limit: int = 100
    max_pages: int = 10
    feed_filter: str = "posts_with_media"
    max_retries: int = 4
    retry_backoff_seconds: float = 0.5
    retry_backoff_max_seconds: float = 15.0
    max_connections: int = 20
    max_keepalive_connections: int = 10

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        return normalized

    @field_validator("page_limit")
    @classmethod
    def _valid_page_limit(cls, value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError("page_limit must be between 1 and 100")
        return value

    @field_validator("max_pages")
    @classmethod
    def _valid_max_pages(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_pages must be positive")
        return value

    @model_validator(mode="after")
    def _valid_retry_policy(self) -> "BlueskyAPIConfig":
        if self.max_retries < 0:
            raise ValueError("api.max_retries must be non-negative")
        if self.retry_backoff_seconds < 0:
            raise ValueError("api.retry_backoff_seconds must be non-negative")
        if self.retry_backoff_max_seconds <= 0:
            raise ValueError("api.retry_backoff_max_seconds must be positive")
        if self.max_connections <= 0:
            raise ValueError("api.max_connections must be positive")
        if self.max_keepalive_connections < 0:
            raise ValueError("api.max_keepalive_connections must be non-negative")
        if self.max_keepalive_connections > self.max_connections:
            raise ValueError("api.max_keepalive_connections must not exceed max_connections")
        return self


class BlueskyStorageConfig(BaseModel):
    candidates_jsonl_path: Path = Path("data/bluesky/image_candidates.jsonl")
    checkpoint_jsonl_path: Path = Path("data/bluesky/collection_checkpoints.jsonl")


class BlueskyModerationConfig(BaseModel):
    public_safe_mode: bool = True
    excluded_labels: list[str] = Field(
        default_factory=lambda: [
            "!hide",
            "!warn",
            "!no-unauthenticated",
            "porn",
            "sexual",
            "sexual-figurative",
            "nudity",
            "graphic-media",
            "gore",
        ]
    )
    excluded_text_terms: list[str] = Field(
        default_factory=lambda: [
            "18+",
            "explicit",
            "lewd",
            "nsfw",
            "porn",
            "yiff",
            "\U0001f51e",
        ]
    )

    @field_validator("excluded_labels", "excluded_text_terms")
    @classmethod
    def _normalized_non_empty_values(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            item = value.strip().lower()
            if not item:
                raise ValueError("moderation exclusions must not contain empty values")
            if item not in normalized:
                normalized.append(item)
        return normalized


class BlueskyConfig(BaseModel):
    version: str = "1.0.0"
    api: BlueskyAPIConfig = Field(default_factory=BlueskyAPIConfig)
    moderation: BlueskyModerationConfig = Field(default_factory=BlueskyModerationConfig)
    storage: BlueskyStorageConfig = Field(default_factory=BlueskyStorageConfig)
    config_hash: str = ""

    @field_validator("version")
    @classmethod
    def _version_required(cls, value: str) -> str:
        if not value:
            raise ValueError("version is required")
        return value


def load_bluesky_config(path: str | Path = "configs/bluesky.default.toml") -> BlueskyConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    config = BlueskyConfig.model_validate(raw)
    payload = config.model_dump(mode="json", exclude={"config_hash"})
    config.config_hash = stable_json_hash(payload)
    return config
