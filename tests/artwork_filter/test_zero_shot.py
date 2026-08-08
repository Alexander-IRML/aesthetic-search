import numpy as np

from artsearch.artwork_filter.enums import ContentClass
from artsearch.artwork_filter.errors import PersistenceError
from artsearch.artwork_filter.prompt_bank import PROMPTED_CLASSES, PromptBank
from artsearch.artwork_filter.zero_shot import ZeroShotArtworkClassifier


class FakeBackend:
    model_id = "fake-siglip"
    model_revision = "revision"
    embedding_dimension = 2

    def encode_images(self, images):
        return np.ones((len(images), 2), dtype=np.float32)

    def encode_texts(self, texts):
        return np.ones((len(texts), 2), dtype=np.float32)

    def score(self, image_embeddings, text_embeddings):
        logits = np.zeros((len(image_embeddings), len(text_embeddings)), dtype=np.float32)
        logits[:, 0] = 8.0
        return logits


class FailingPromptStore:
    def get(self, key):
        return None

    def put(self, key, array, metadata):
        raise PersistenceError("read-only cache")


def test_zero_shot_returns_every_class_in_stable_order():
    bank = PromptBank(
        version="test",
        top_k=1,
        classes={
            content_class: [f"prompt {content_class.value}"] for content_class in PROMPTED_CLASSES
        },
    )
    classifier = ZeroShotArtworkClassifier(FakeBackend(), bank)

    result = classifier.classify_embeddings(np.asarray([[1.0, 0.0]], dtype=np.float32))[0]

    assert [score.content_class for score in result.class_scores] == list(PROMPTED_CLASSES)
    assert max(result.class_scores, key=lambda item: item.score).content_class == (
        ContentClass.FINISHED_ILLUSTRATION
    )
    assert result.art_utility_score > result.noise_score
    assert result.confidence_margin > 0.8

    matches = classifier.inspect_embedding(np.asarray([1.0, 0.0]), top_k=1)
    assert matches[0].content_class == ContentClass.FINISHED_ILLUSTRATION


def test_zero_shot_continues_when_prompt_cache_is_not_writable():
    bank = PromptBank(
        version="test",
        top_k=1,
        classes={
            content_class: [f"prompt {content_class.value}"] for content_class in PROMPTED_CLASSES
        },
    )
    classifier = ZeroShotArtworkClassifier(
        FakeBackend(),
        bank,
        prompt_store=FailingPromptStore(),
    )

    results = classifier.classify_embeddings(np.asarray([[1.0, 0.0]], dtype=np.float32))

    assert len(results) == 1
