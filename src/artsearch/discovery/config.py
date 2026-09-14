from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib

from pydantic import BaseModel, Field, field_validator, model_validator


class DiscoverySourceConfig(BaseModel):
    manual_seeds: bool = True
    actor_search: bool = True
    post_search: bool = True
    starter_pack_search: bool = True
    outbound_follows: bool = True
    suggested_follows: bool = True
    custom_feeds: bool = False
    jetstream: bool = False
    actor_queries: list[str] = Field(default_factory=list)
    post_queries: list[str] = Field(default_factory=list)
    starter_pack_queries: list[str] = Field(default_factory=list)

    @field_validator("actor_queries", "post_queries", "starter_pack_queries")
    @classmethod
    def _normalize_queries(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(normalized))


class DiscoveryCrawlConfig(BaseModel):
    concurrency: int = 8
    max_depth: int = 2
    max_api_requests_per_run: int = 1000
    daily_creator_budget: int = 200
    daily_media_budget: int = 5000
    max_tasks_per_run: int = 500
    max_expansion_candidates_per_creator: int = 25
    max_outbound_follows_per_creator: int = 25
    max_suggested_follows_per_creator: int = 25
    max_starter_packs_per_query: int = 10
    max_list_members_per_pack: int = 100
    max_search_results_per_query: int = 50
    initial_media_sample: int = 25
    deep_media_sample: int = 100
    task_max_attempts: int = 3
    task_lease_seconds: int = 900
    retry_base_seconds: int = 30
    exploration_fraction: float = 0.15

    @model_validator(mode="after")
    def _validate_limits(self) -> "DiscoveryCrawlConfig":
        integer_limits = (
            self.concurrency,
            self.max_api_requests_per_run,
            self.daily_creator_budget,
            self.daily_media_budget,
            self.max_tasks_per_run,
            self.max_expansion_candidates_per_creator,
            self.max_outbound_follows_per_creator,
            self.max_suggested_follows_per_creator,
            self.max_starter_packs_per_query,
            self.max_list_members_per_pack,
            self.max_search_results_per_query,
            self.initial_media_sample,
            self.deep_media_sample,
            self.task_max_attempts,
            self.task_lease_seconds,
            self.retry_base_seconds,
        )
        if any(value <= 0 for value in integer_limits):
            raise ValueError("crawl limits must be positive")
        if self.max_depth < 0:
            raise ValueError("crawl.max_depth must be non-negative")
        if self.deep_media_sample < self.initial_media_sample:
            raise ValueError("deep_media_sample must be at least initial_media_sample")
        if not 0.0 <= self.exploration_fraction <= 1.0:
            raise ValueError("exploration_fraction must be within [0, 1]")
        return self


class QualificationConfig(BaseModel):
    minimum_evaluated_media: int = 5
    auto_qualify_score: float = 0.72
    reject_below_score: float = 0.30
    minimum_art_rate: float = 0.45
    minimum_accept_rate: float = 0.20
    review_reposts: bool = True
    resample_after_days: int = 30
    reconsider_rejected_after_days: int = 90

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "QualificationConfig":
        if self.minimum_evaluated_media <= 0:
            raise ValueError("minimum_evaluated_media must be positive")
        if self.resample_after_days <= 0 or self.reconsider_rejected_after_days <= 0:
            raise ValueError("creator resampling intervals must be positive")
        ratios = (
            self.auto_qualify_score,
            self.reject_below_score,
            self.minimum_art_rate,
            self.minimum_accept_rate,
        )
        if any(not 0.0 <= value <= 1.0 for value in ratios):
            raise ValueError("qualification thresholds must be within [0, 1]")
        if self.reject_below_score >= self.auto_qualify_score:
            raise ValueError("reject_below_score must be below auto_qualify_score")
        return self


class DiscoveryScoreConfig(BaseModel):
    version: str = "creator-score-v1"
    art_relevance_weight: float = 0.30
    portfolio_density_weight: float = 0.20
    visual_quality_weight: float = 0.15
    creator_identity_weight: float = 0.10
    source_reliability_weight: float = 0.10
    novelty_weight: float = 0.10
    activity_weight: float = 0.05
    repost_penalty_weight: float = 0.15
    popularity_penalty_weight: float = 0.03
    long_tail_bonus_weight: float = 0.05

    @model_validator(mode="after")
    def _validate_weights(self) -> "DiscoveryScoreConfig":
        positive = (
            self.art_relevance_weight,
            self.portfolio_density_weight,
            self.visual_quality_weight,
            self.creator_identity_weight,
            self.source_reliability_weight,
            self.novelty_weight,
            self.activity_weight,
        )
        penalties = (
            self.repost_penalty_weight,
            self.popularity_penalty_weight,
            self.long_tail_bonus_weight,
        )
        if any(value < 0 for value in (*positive, *penalties)):
            raise ValueError("discovery score weights must be non-negative")
        if abs(sum(positive) - 1.0) > 1e-6:
            raise ValueError("positive discovery score weights must sum to 1.0")
        if not self.version.strip():
            raise ValueError("score version is required")
        return self


class DiscoverySafetyConfig(BaseModel):
    require_source_moderation: bool = True
    block_profile_labels: list[str] = Field(
        default_factory=lambda: [
            "!hide",
            "!no-unauthenticated",
            "porn",
            "sexual",
            "nudity",
            "graphic-media",
            "gore",
        ]
    )
    review_profile_terms: list[str] = Field(
        default_factory=lambda: ["18+", "adult art", "nsfw", "explicit", "porn", "yiff"]
    )

    @field_validator("block_profile_labels", "review_profile_terms")
    @classmethod
    def _normalize_policy_values(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().casefold() for value in values if value.strip()]
        return list(dict.fromkeys(normalized))


class DiscoveryRetentionConfig(BaseModel):
    retain_rejected_image_bytes: bool = False
    hot_event_days: int = 30
    archive_events_to_object_store: bool = True

    @field_validator("hot_event_days")
    @classmethod
    def _positive_days(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("hot_event_days must be positive")
        return value


class DiscoveryConfig(BaseModel):
    version: str = "1.0.0"
    sources: DiscoverySourceConfig = Field(default_factory=DiscoverySourceConfig)
    crawl: DiscoveryCrawlConfig = Field(default_factory=DiscoveryCrawlConfig)
    qualification: QualificationConfig = Field(default_factory=QualificationConfig)
    scoring: DiscoveryScoreConfig = Field(default_factory=DiscoveryScoreConfig)
    safety: DiscoverySafetyConfig = Field(default_factory=DiscoverySafetyConfig)
    retention: DiscoveryRetentionConfig = Field(default_factory=DiscoveryRetentionConfig)
    config_hash: str = ""

    @field_validator("version")
    @classmethod
    def _version_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("discovery configuration version is required")
        return value.strip()


def load_discovery_config(
    path: str | Path = "configs/discovery.default.toml",
) -> DiscoveryConfig:
    source = Path(path)
    with source.open("rb") as handle:
        raw = tomllib.load(handle)
    config = DiscoveryConfig.model_validate(raw)
    return _with_config_hash(config)


def override_discovery_limits(
    config: DiscoveryConfig,
    *,
    creator_budget: int | None = None,
    media_budget: int | None = None,
    max_tasks: int | None = None,
    max_depth: int | None = None,
    request_budget: int | None = None,
) -> DiscoveryConfig:
    """Return a re-hashed run configuration after explicit CLI/DAG overrides."""

    updates = {
        key: value
        for key, value in {
            "daily_creator_budget": creator_budget,
            "daily_media_budget": media_budget,
            "max_tasks_per_run": max_tasks,
            "max_depth": max_depth,
            "max_api_requests_per_run": request_budget,
        }.items()
        if value is not None
    }
    crawl = config.crawl.model_copy(update=updates)
    resolved = config.model_copy(update={"crawl": crawl, "config_hash": ""})
    resolved = DiscoveryConfig.model_validate(resolved.model_dump())
    return _with_config_hash(resolved)


def _with_config_hash(config: DiscoveryConfig) -> DiscoveryConfig:
    payload = config.model_dump(mode="json", exclude={"config_hash"})
    config.config_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return config
