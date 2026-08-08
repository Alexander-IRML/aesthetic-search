import asyncio

import numpy as np
from PIL import Image

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.enums import ContentClass, FilterDecision, ModelMode
from artsearch.artwork_filter.feature_store import LocalNumpyFeatureStore
from artsearch.artwork_filter.schemas import ClassScore, ImageCandidate, VisualScores
from artsearch.artwork_filter.service import ArtworkFilterService


class FakeVisualClassifier:
    prompt_version = "test-prompts"
    model_id = "test-model"
    model_revision = "test-revision"
    embedding_dimension = 2

    def __init__(self):
        self.encode_calls = 0

    def encode_images(self, images):
        self.encode_calls += 1
        return np.asarray([[1.0, 0.0] for _ in images], dtype=np.float32)

    def classify_embeddings(self, image_embeddings, *, cache_keys=None):
        return [
            VisualScores(
                backend="fake",
                model_id=self.model_id,
                model_revision=self.model_revision,
                mode=ModelMode.ZERO_SHOT,
                class_scores=[
                    ClassScore(content_class=ContentClass.FINISHED_ILLUSTRATION, score=0.99)
                ],
                art_utility_score=0.99,
                noise_score=0.01,
                confidence_margin=0.9,
                embedding_dimension=2,
                embedding_cache_key=cache_keys[index] if cache_keys else None,
            )
            for index in range(len(image_embeddings))
        ]


def test_service_uses_cached_image_embedding_on_repeat(tmp_path):
    image_path = tmp_path / "image.jpg"
    Image.new("RGB", (512, 512), (20, 40, 60)).save(image_path)
    config = load_artwork_filter_config()
    config.media.review_low_variance = False
    config.policy.automatic_accept_enabled = True
    classifier = FakeVisualClassifier()
    service = ArtworkFilterService(
        config,
        visual_classifier=classifier,
        feature_store=LocalNumpyFeatureStore(tmp_path / "features"),
    )
    candidate = ImageCandidate(candidate_id="candidate", local_path=image_path, source="test")

    first = asyncio.run(service.classify(candidate))
    second = asyncio.run(service.classify(candidate))

    assert first.decision == FilterDecision.ACCEPT
    assert second.decision == FilterDecision.ACCEPT
    assert classifier.encode_calls == 1
    assert first.visual_scores.embedding_cache_key == second.visual_scores.embedding_cache_key


def test_default_policy_routes_high_scoring_art_to_review_until_accept_is_enabled(tmp_path):
    image_path = tmp_path / "image.jpg"
    Image.new("RGB", (512, 512), (20, 40, 60)).save(image_path)
    config = load_artwork_filter_config()
    config.media.review_low_variance = False
    service = ArtworkFilterService(config, visual_classifier=FakeVisualClassifier())

    result = asyncio.run(
        service.classify(
            ImageCandidate(candidate_id="candidate", local_path=image_path, source="test")
        )
    )

    assert result.decision == FilterDecision.REVIEW
    assert result.accepted_for_main_corpus is False
    assert result.reason_codes == ["review.automatic_accept_disabled"]
