from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ImageConfig:
    canonical_size: int
    crop_threshold: float
    output_format: str
    jpeg_quality: int
    padding_fill_strategy: str
    neutral_gray_value: int


@dataclass(frozen=True)
class DuplicateConfig:
    phash_distance_threshold: int


@dataclass(frozen=True)
class AppConfig:
    root_dir: Path
    raw_dir: Path
    processed_dir: Path
    database_path: Path
    images: ImageConfig
    duplicates: DuplicateConfig


def load_config(path: str | Path = "config/config.yaml") -> AppConfig:
    config_path = Path(path)
    root_dir = config_path.resolve().parents[1]
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    paths = raw.get("paths", {})
    images = raw.get("images", {})
    duplicates = raw.get("duplicates", {})

    image_config = ImageConfig(
        canonical_size=int(images.get("canonical_size", 448)),
        crop_threshold=float(images.get("crop_threshold", 2.5)),
        output_format=str(images.get("output_format", "jpeg")).lower(),
        jpeg_quality=int(images.get("jpeg_quality", 95)),
        padding_fill_strategy=str(images.get("padding_fill_strategy", "neutral_gray")),
        neutral_gray_value=int(images.get("neutral_gray_value", 128)),
    )
    duplicate_config = DuplicateConfig(
        phash_distance_threshold=int(duplicates.get("phash_distance_threshold", 6)),
    )

    _validate_image_config(image_config)

    return AppConfig(
        root_dir=root_dir,
        raw_dir=_resolve(root_dir, paths.get("raw_dir", "data/raw")),
        processed_dir=_resolve(root_dir, paths.get("processed_dir", "data/processed")),
        database_path=_resolve(root_dir, paths.get("database", "data/artsearch.db")),
        images=image_config,
        duplicates=duplicate_config,
    )


def _resolve(root_dir: Path, value: Any) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    return root_dir / path


def _validate_image_config(config: ImageConfig) -> None:
    if config.canonical_size <= 0:
        raise ValueError("images.canonical_size must be positive")
    if config.canonical_size % 14 != 0:
        raise ValueError("images.canonical_size must be divisible by 14")
    if config.crop_threshold < 1:
        raise ValueError("images.crop_threshold must be >= 1")
    if config.output_format not in {"jpeg", "jpg"}:
        raise ValueError("images.output_format currently supports only jpeg")
    if not 1 <= config.jpeg_quality <= 100:
        raise ValueError("images.jpeg_quality must be between 1 and 100")
    if config.padding_fill_strategy not in {"neutral_gray", "average_edge_color"}:
        raise ValueError(
            "images.padding_fill_strategy must be neutral_gray or average_edge_color"
        )
    if not 0 <= config.neutral_gray_value <= 255:
        raise ValueError("images.neutral_gray_value must be between 0 and 255")