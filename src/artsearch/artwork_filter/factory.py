from __future__ import annotations

from pathlib import Path

from artsearch.artwork_filter.config import ArtworkFilterConfig
from artsearch.artwork_filter.enums import ModelMode
from artsearch.artwork_filter.errors import ModelArtifactError
from artsearch.artwork_filter.feature_store import LocalNumpyFeatureStore
from artsearch.artwork_filter.image_io import ImageLoader
from artsearch.artwork_filter.persistence import DecisionStore
from artsearch.artwork_filter.prompt_bank import load_prompt_bank
from artsearch.artwork_filter.service import ArtworkFilterService
from artsearch.artwork_filter.siglip_backend import Siglip2Backend
from artsearch.artwork_filter.zero_shot import ZeroShotArtworkClassifier


def build_artwork_filter_service(
    config: ArtworkFilterConfig,
    *,
    prompt_config: str | Path = "configs/artwork_filter.prompts.v1.toml",
    decision_store: DecisionStore | None = None,
    image_loader: ImageLoader | None = None,
    deterministic_only: bool = False,
) -> ArtworkFilterService:
    feature_store = LocalNumpyFeatureStore(config.storage.cache_dir / "features")
    visual_classifier = None
    if not deterministic_only:
        if config.mode != ModelMode.ZERO_SHOT:
            raise ModelArtifactError(
                f"model mode {config.mode.value!r} requires a configured classifier artifact"
            )
        backend = Siglip2Backend(config.model)
        visual_classifier = ZeroShotArtworkClassifier(
            backend,
            load_prompt_bank(prompt_config),
            prompt_store=feature_store,
            normalize_embeddings=config.model.normalize_embeddings,
        )
    return ArtworkFilterService(
        config,
        image_loader=image_loader,
        visual_classifier=visual_classifier,
        feature_store=feature_store,
        decision_store=decision_store,
    )
