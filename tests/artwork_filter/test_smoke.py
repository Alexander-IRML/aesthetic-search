from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from artsearch.artwork_filter.cli import _validate_smoke_paths
from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.enums import ContentClass, ModelMode
from artsearch.artwork_filter.schemas import ClassScore, VisualScores
from artsearch.artwork_filter.service import ArtworkFilterService
from artsearch.artwork_filter.smoke import run_model_smoke_test


class FakeVisualClassifier:
    prompt_version = "test-prompts"
    model_id = "test/model"
    model_revision = "test-revision"
    embedding_dimension = 2

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        return np.asarray(
            [[float(image.size[0]), float(image.size[1])] for image in images],
            dtype=np.float32,
        )

    def classify_embeddings(
        self,
        image_embeddings: np.ndarray,
        *,
        cache_keys: Sequence[str | None] | None = None,
    ) -> list[VisualScores]:
        return [
            VisualScores(
                backend="fake",
                model_id=self.model_id,
                model_revision=self.model_revision,
                mode=ModelMode.ZERO_SHOT,
                class_scores=[
                    ClassScore(
                        content_class=ContentClass.FINISHED_ILLUSTRATION,
                        score=1.0,
                    )
                ],
                art_utility_score=1.0,
                noise_score=0.0,
                confidence_margin=1.0,
                embedding_dimension=2,
            )
            for _ in image_embeddings
        ]


def test_model_smoke_test_checks_batch_consistency_without_exposing_parent_paths(tmp_path):
    first = _write_image(tmp_path / "first.jpg", (320, 320))
    second = _write_image(tmp_path / "second.jpg", (640, 480))
    service = ArtworkFilterService(
        load_artwork_filter_config(),
        visual_classifier=FakeVisualClassifier(),
    )

    report = asyncio.run(run_model_smoke_test(service, [first, second]))

    assert report["all_finite"] is True
    assert report["passed"] is True
    assert report["batch_single_consistent"] is True
    assert report["batch_single_max_abs_delta"] == 0.0
    assert [item["filename"] for item in report["items"]] == ["first.jpg", "second.jpg"]


def test_smoke_path_validation_reports_missing_input_without_loading_model(tmp_path):
    missing = tmp_path / "missing.jpg"

    with pytest.raises(ValueError, match="smoke-test image does not exist"):
        _validate_smoke_paths([str(missing)])


def _write_image(path: Path, size: tuple[int, int]) -> Path:
    Image.new("RGB", size, (32, 64, 96)).save(path)
    return path
