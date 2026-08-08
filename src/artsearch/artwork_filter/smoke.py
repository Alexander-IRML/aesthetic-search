from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.artwork_filter.service import ArtworkFilterService


async def run_model_smoke_test(
    service: ArtworkFilterService,
    paths: list[str | Path],
    *,
    consistency_tolerance: float = 1e-4,
) -> dict[str, Any]:
    """Exercise real image and prompt inference without persisting decisions."""
    if not paths:
        raise ValueError("at least one smoke-test image is required")
    if consistency_tolerance < 0:
        raise ValueError("consistency_tolerance must be non-negative")
    classifier = service.visual_classifier
    if classifier is None:
        raise RuntimeError("model smoke testing requires a visual classifier")

    candidates = [
        ImageCandidate(
            candidate_id=f"smoke:{index}",
            local_path=Path(path),
            source="local",
        )
        for index, path in enumerate(paths)
    ]
    loaded = []
    try:
        for candidate in candidates:
            loaded.append(await service.image_loader.load(candidate))
        batch = np.asarray(
            classifier.encode_images([item.rgb_image for item in loaded]),
            dtype=np.float32,
        )
        singles = np.concatenate(
            [
                np.asarray(classifier.encode_images([item.rgb_image]), dtype=np.float32)
                for item in loaded
            ],
            axis=0,
        )
        expected_shape = (len(loaded), classifier.embedding_dimension)
        if batch.shape != expected_shape or singles.shape != expected_shape:
            raise RuntimeError(
                f"unexpected embedding shape: batch={batch.shape}, singles={singles.shape}"
            )
        if not np.isfinite(batch).all() or not np.isfinite(singles).all():
            raise RuntimeError("model produced non-finite image embeddings")

        maximum_delta = float(np.max(np.abs(batch - singles)))
        visual_scores = classifier.classify_embeddings(batch)
        if len(visual_scores) != len(loaded):
            raise RuntimeError("visual classifier returned the wrong smoke-test result count")
        consistent = maximum_delta <= consistency_tolerance
        items = []
        for path, embedding, scores in zip(paths, batch, visual_scores, strict=True):
            top = max(scores.class_scores, key=lambda item: item.score)
            items.append(
                {
                    "filename": Path(path).name,
                    "embedding_norm": float(np.linalg.norm(embedding)),
                    "predicted_class": top.content_class.value,
                    "class_score": top.score,
                    "art_utility_score": scores.art_utility_score,
                    "noise_score": scores.noise_score,
                    "confidence_margin": scores.confidence_margin,
                }
            )
        return {
            "model_id": classifier.model_id,
            "model_revision": classifier.model_revision,
            "prompt_version": classifier.prompt_version,
            "embedding_dimension": classifier.embedding_dimension,
            "image_count": len(loaded),
            "all_finite": True,
            "batch_single_max_abs_delta": maximum_delta,
            "consistency_tolerance": consistency_tolerance,
            "batch_single_consistent": consistent,
            "passed": consistent,
            "items": items,
        }
    finally:
        for item in loaded:
            item.rgb_image.close()
