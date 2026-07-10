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
class ModelConfig:
    clip_model_name: str
    clip_model_version: str
    dino_model_name: str
    dino_model_version: str


@dataclass(frozen=True)
class EmbeddingConfig:
    batch_size: int
    device: str


@dataclass(frozen=True)
class RetrievalConfig:
    default_top_k: int
    demo_output_path: Path
    gallery_output_path: Path


@dataclass(frozen=True)
class AppConfig:
    root_dir: Path
    raw_dir: Path
    processed_dir: Path
    database_path: Path
    images: ImageConfig
    duplicates: DuplicateConfig
    models: ModelConfig
    embeddings: EmbeddingConfig
    retrieval: RetrievalConfig


def load_config(path: str | Path = "config/config.yaml") -> AppConfig:
    config_path = Path(path)
    root_dir = config_path.resolve().parents[1]
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    paths = raw.get("paths", {})
    images = raw.get("images", {})
    duplicates = raw.get("duplicates", {})
    models = raw.get("models", {})
    embeddings = raw.get("embeddings", {})
    retrieval = raw.get("retrieval", {})

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
    model_config = ModelConfig(
        clip_model_name=str(models.get("clip_model_name", "openai/clip-vit-base-patch32")),
        clip_model_version=str(models.get("clip_model_version", "main")),
        dino_model_name=str(
            models.get("dino_model_name", "facebook/dinov2-with-registers-base")
        ),
        dino_model_version=str(models.get("dino_model_version", "main")),
    )
    embedding_config = EmbeddingConfig(
        batch_size=int(embeddings.get("batch_size", 4)),
        device=str(embeddings.get("device", "cpu")),
    )
    retrieval_config = RetrievalConfig(
        default_top_k=int(retrieval.get("default_top_k", 12)),
        demo_output_path=_resolve(root_dir, retrieval.get("demo_output", "data/search_demo.html")),
        gallery_output_path=_resolve(
            root_dir,
            retrieval.get("gallery_output", "data/search_gallery.html"),
        ),
    )

    _validate_image_config(image_config)
    _validate_model_config(model_config)
    _validate_embedding_config(embedding_config)
    _validate_retrieval_config(retrieval_config)

    return AppConfig(
        root_dir=root_dir,
        raw_dir=_resolve(root_dir, paths.get("raw_dir", "data/raw")),
        processed_dir=_resolve(root_dir, paths.get("processed_dir", "data/processed")),
        database_path=_resolve(root_dir, paths.get("database", "data/artsearch.db")),
        images=image_config,
        duplicates=duplicate_config,
        models=model_config,
        embeddings=embedding_config,
        retrieval=retrieval_config,
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


def _validate_model_config(config: ModelConfig) -> None:
    if not config.clip_model_name:
        raise ValueError("models.clip_model_name must be set")
    if not config.clip_model_version:
        raise ValueError("models.clip_model_version must be set")
    if not config.dino_model_name:
        raise ValueError("models.dino_model_name must be set")
    if not config.dino_model_version:
        raise ValueError("models.dino_model_version must be set")


def _validate_embedding_config(config: EmbeddingConfig) -> None:
    if config.batch_size <= 0:
        raise ValueError("embeddings.batch_size must be positive")
    if config.device not in {"cpu", "cuda"}:
        raise ValueError("embeddings.device must be cpu or cuda")


def _validate_retrieval_config(config: RetrievalConfig) -> None:
    if config.default_top_k <= 0:
        raise ValueError("retrieval.default_top_k must be positive")
