from __future__ import annotations

from pathlib import Path
import tomllib

from pydantic import BaseModel, Field, field_validator, model_validator

from artsearch.artwork_filter.enums import ModelMode
from artsearch.artwork_filter.hashing import stable_json_hash


class ModelConfig(BaseModel):
    model_id: str = "google/siglip2-base-patch16-224"
    revision: str = ""
    device: str = "auto"
    dtype: str = "auto"
    batch_size: int = 32
    compile_model: bool = False
    cache_embeddings: bool = True
    normalize_embeddings: bool = True
    preprocessing_version: str = "siglip2-fixres-v1"

    @field_validator("model_id", "preprocessing_version")
    @classmethod
    def _non_empty_model_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model identifiers and versions must be non-empty")
        return value

    @field_validator("device")
    @classmethod
    def _known_device(cls, value: str) -> str:
        if value not in {"auto", "cpu", "cuda", "mps"}:
            raise ValueError("model.device must be auto, cpu, cuda, or mps")
        return value

    @field_validator("dtype")
    @classmethod
    def _known_dtype(cls, value: str) -> str:
        if value not in {"auto", "float32", "float16", "bfloat16"}:
            raise ValueError("model.dtype must be auto, float32, float16, or bfloat16")
        return value

    @field_validator("batch_size")
    @classmethod
    def _positive_batch_size(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("model.batch_size must be positive")
        return value


class DownloadConfig(BaseModel):
    timeout_seconds: float = 20
    max_bytes: int = 15_000_000
    max_retries: int = 2
    prefer_thumbnail: bool = True
    allow_fullsize_fallback: bool = True
    user_agent: str = "ArtSearchResearch/0.1"
    max_concurrency: int = 8
    max_redirects: int = 5
    allowed_bluesky_image_hosts: list[str] = Field(default_factory=lambda: ["cdn.bsky.app"])

    @model_validator(mode="after")
    def _positive_limits(self) -> "DownloadConfig":
        if self.timeout_seconds <= 0:
            raise ValueError("downloads.timeout_seconds must be positive")
        if self.max_bytes <= 0:
            raise ValueError("downloads.max_bytes must be positive")
        if self.max_retries < 0:
            raise ValueError("downloads.max_retries must be non-negative")
        if self.max_concurrency <= 0:
            raise ValueError("downloads.max_concurrency must be positive")
        if self.max_redirects < 0:
            raise ValueError("downloads.max_redirects must be non-negative")
        if not self.allowed_bluesky_image_hosts or any(
            not host.strip() for host in self.allowed_bluesky_image_hosts
        ):
            raise ValueError("downloads.allowed_bluesky_image_hosts must not be empty")
        return self


class MediaConfig(BaseModel):
    min_width: int = 256
    min_height: int = 256
    min_area: int = 100_000
    max_aspect_ratio: float = 8.0
    allow_animated: bool = False
    convert_rgba_to_rgb_background: str = "white"
    reject_corrupt: bool = True
    review_low_variance: bool = True
    low_variance_threshold: float = 2.0
    max_pixels: int = 100_000_000

    @model_validator(mode="after")
    def _valid_media_limits(self) -> "MediaConfig":
        if min(self.min_width, self.min_height, self.min_area, self.max_pixels) <= 0:
            raise ValueError("media size limits must be positive")
        if self.max_aspect_ratio < 1.0:
            raise ValueError("media.max_aspect_ratio must be at least 1")
        if self.low_variance_threshold < 0.0:
            raise ValueError("media.low_variance_threshold must be non-negative")
        return self


class PolicyConfig(BaseModel):
    automatic_accept_enabled: bool = False
    accept_finished_illustration: bool = True
    accept_traditional_art: bool = True
    accept_comic: bool = True
    accept_character_sheet: bool = True
    accept_sketch_or_wip: bool = False
    accept_three_d_render: bool = True
    accept_photo_of_art: bool = False
    accept_art_merch_photo: bool = False
    accept_commission_sheet: bool = False
    accept_adoptable_sheet: bool = False


class ThresholdConfig(BaseModel):
    accept_score: float = 0.78
    reject_score: float = 0.35
    minimum_margin: float = 0.12
    force_review_below_confidence: float = 0.65

    @model_validator(mode="after")
    def _valid_thresholds(self) -> "ThresholdConfig":
        values = (
            self.accept_score,
            self.reject_score,
            self.minimum_margin,
            self.force_review_below_confidence,
        )
        if any(value < 0.0 or value > 1.0 for value in values):
            raise ValueError("artwork-filter thresholds must be in [0, 1]")
        if self.reject_score >= self.accept_score:
            raise ValueError("thresholds.reject_score must be below accept_score")
        return self


class EnsembleConfig(BaseModel):
    visual_weight: float = 0.85
    text_weight: float = 0.10
    rule_adjustment_weight: float = 0.05

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "EnsembleConfig":
        if any(
            value < 0.0 or value > 1.0
            for value in (
                self.visual_weight,
                self.text_weight,
                self.rule_adjustment_weight,
            )
        ):
            raise ValueError("ensemble weights must be in [0, 1]")
        total = self.visual_weight + self.text_weight + self.rule_adjustment_weight
        if abs(total - 1.0) > 0.0001:
            raise ValueError("ensemble weights must sum to 1.0")
        return self


class RepostConfig(BaseModel):
    force_review_reposts: bool = True
    reject_known_other_artist_reposts: bool = False


class StorageConfig(BaseModel):
    decision_jsonl_path: Path = Path("data/filter/decisions.jsonl")
    cache_dir: Path = Path("data/cache/artwork_filter")
    review_image_dir: Path = Path("data/filter/review_images")
    download_review_images: bool = False
    durability_mode: str = "strict"

    @field_validator("durability_mode")
    @classmethod
    def _known_durability_mode(cls, value: str) -> str:
        if value not in {"strict", "best_effort"}:
            raise ValueError("storage.durability_mode must be strict or best_effort")
        return value


class ArtworkFilterConfig(BaseModel):
    version: str = "1.0.0"
    mode: ModelMode = ModelMode.ZERO_SHOT
    model: ModelConfig = Field(default_factory=ModelConfig)
    downloads: DownloadConfig = Field(default_factory=DownloadConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    ensemble: EnsembleConfig = Field(default_factory=EnsembleConfig)
    reposts: RepostConfig = Field(default_factory=RepostConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    config_hash: str = ""

    @field_validator("version")
    @classmethod
    def _version_required(cls, value: str) -> str:
        if not value:
            raise ValueError("version is required")
        return value


def load_artwork_filter_config(
    path: str | Path = "configs/artwork_filter.default.toml",
) -> ArtworkFilterConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    config = ArtworkFilterConfig.model_validate(raw)
    payload = config.model_dump(mode="json", exclude={"config_hash"})
    config.config_hash = stable_json_hash(payload)
    return config
