from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import logging
from math import exp, log

import numpy as np
from PIL import Image

from artsearch.artwork_filter.enums import ContentClass, ModelMode
from artsearch.artwork_filter.errors import PersistenceError
from artsearch.artwork_filter.feature_store import FeatureStore, prompt_feature_cache_key
from artsearch.artwork_filter.prompt_bank import PROMPTED_CLASSES, PromptBank
from artsearch.artwork_filter.schemas import ClassScore, VisualScores
from artsearch.artwork_filter.siglip_backend import VisionLanguageBackend


LOGGER = logging.getLogger(__name__)


USEFUL_ART_CLASSES = (
    ContentClass.FINISHED_ILLUSTRATION,
    ContentClass.TRADITIONAL_ART,
    ContentClass.COMIC,
    ContentClass.CHARACTER_SHEET,
    ContentClass.SKETCH_OR_WIP,
    ContentClass.THREE_D_RENDER,
)
ROUTING_CLASSES = (
    ContentClass.COMMISSION_SHEET,
    ContentClass.ADOPTABLE_SHEET,
    ContentClass.ART_MERCH_PHOTO,
    ContentClass.PHOTO_OF_ART,
)
NOISE_CLASSES = (
    ContentClass.CASUAL_PHOTO,
    ContentClass.SELFIE,
    ContentClass.FOOD_PHOTO,
    ContentClass.PET_PHOTO,
    ContentClass.SCREENSHOT,
    ContentClass.MEME,
    ContentClass.TEXT_ANNOUNCEMENT,
    ContentClass.OTHER,
)


@dataclass(frozen=True)
class PromptMatch:
    content_class: ContentClass
    prompt: str
    score: float


class ZeroShotArtworkClassifier:
    """Aggregate multiple stable prompts per class over frozen image embeddings."""

    def __init__(
        self,
        backend: VisionLanguageBackend,
        prompt_bank: PromptBank,
        *,
        prompt_store: FeatureStore | None = None,
        normalize_embeddings: bool = True,
    ) -> None:
        self.backend = backend
        self.prompt_bank = prompt_bank
        self.prompt_store = prompt_store
        self.normalize_embeddings = normalize_embeddings
        self._prompt_embeddings: np.ndarray | None = None
        self._memberships: list[ContentClass] = []

    @property
    def prompt_version(self) -> str:
        return self.prompt_bank.version

    @property
    def model_id(self) -> str:
        return self.backend.model_id

    @property
    def model_revision(self) -> str | None:
        return self.backend.model_revision

    @property
    def embedding_dimension(self) -> int:
        return self.backend.embedding_dimension

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        return self.backend.encode_images(images)

    def classify_embeddings(
        self,
        image_embeddings: np.ndarray,
        *,
        cache_keys: Sequence[str | None] | None = None,
    ) -> list[VisualScores]:
        images = np.asarray(image_embeddings, dtype=np.float32)
        if images.ndim != 2:
            raise ValueError("image_embeddings must be two-dimensional")
        prompt_embeddings = self._get_prompt_embeddings()
        prompt_logits = self.backend.score(images, prompt_embeddings)
        if prompt_logits.shape != (len(images), len(self._memberships)):
            raise ValueError("backend returned an unexpected prompt-score shape")

        keys = list(cache_keys) if cache_keys is not None else [None] * len(images)
        if len(keys) != len(images):
            raise ValueError("cache_keys must match image_embeddings")

        results = []
        for row_index, row in enumerate(prompt_logits):
            class_logits = self._aggregate_class_logits(row)
            probabilities = _softmax(class_logits)
            ordered_probabilities = sorted(probabilities, reverse=True)
            margin = (
                float(ordered_probabilities[0] - ordered_probabilities[1])
                if len(ordered_probabilities) > 1
                else 1.0
            )
            useful_logsum = _logsumexp(_group_values(class_logits, USEFUL_ART_CLASSES))
            routing_logsum = _logsumexp(_group_values(class_logits, ROUTING_CLASSES))
            noise_logsum = _logsumexp(_group_values(class_logits, NOISE_CLASSES))
            utility = _sigmoid(useful_logsum - max(noise_logsum, routing_logsum))
            noise = _sigmoid(noise_logsum - max(useful_logsum, routing_logsum))
            results.append(
                VisualScores(
                    backend="siglip2",
                    model_id=self.backend.model_id,
                    model_revision=self.backend.model_revision,
                    mode=ModelMode.ZERO_SHOT,
                    class_scores=[
                        ClassScore(content_class=content_class, score=float(probability))
                        for content_class, probability in zip(
                            PROMPTED_CLASSES,
                            probabilities,
                            strict=True,
                        )
                    ],
                    art_utility_score=float(utility),
                    noise_score=float(noise),
                    confidence_margin=margin,
                    embedding_dimension=int(images.shape[1]),
                    embedding_cache_key=keys[row_index],
                )
            )
        return results

    def inspect_embedding(
        self,
        image_embedding: np.ndarray,
        *,
        top_k: int = 15,
    ) -> list[PromptMatch]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        prompts, _ = self.prompt_bank.flattened()
        prompt_embeddings = self._get_prompt_embeddings()
        image = np.asarray(image_embedding, dtype=np.float32)
        if image.ndim == 1:
            image = image[None, :]
        if image.shape != (1, self.embedding_dimension):
            raise ValueError("image_embedding has an unexpected shape")
        scores = self.backend.score(image, prompt_embeddings)[0]
        ranked = np.argsort(scores)[::-1][: min(top_k, len(scores))]
        return [
            PromptMatch(
                content_class=self._memberships[index],
                prompt=prompts[index],
                score=float(scores[index]),
            )
            for index in ranked
        ]

    def _get_prompt_embeddings(self) -> np.ndarray:
        if self._prompt_embeddings is not None:
            return self._prompt_embeddings

        prompts, self._memberships = self.prompt_bank.flattened()
        cache_key = prompt_feature_cache_key(
            model_id=self.backend.model_id,
            model_revision=self.backend.model_revision,
            prompt_version=self.prompt_bank.version,
            prompt_hash=self.prompt_bank.prompt_hash,
            normalize_embeddings=self.normalize_embeddings,
        )
        cached = self.prompt_store.get(cache_key) if self.prompt_store is not None else None
        if cached is not None and cached.shape == (len(prompts), self.embedding_dimension):
            self._prompt_embeddings = cached
            return cached

        embeddings = self.backend.encode_texts(prompts)
        if embeddings.shape != (len(prompts), self.embedding_dimension):
            raise ValueError("backend returned an unexpected prompt-embedding shape")
        self._prompt_embeddings = embeddings
        if self.prompt_store is not None:
            try:
                self.prompt_store.put(
                    cache_key,
                    embeddings,
                    {
                        "kind": "prompt_embeddings",
                        "model_id": self.backend.model_id,
                        "model_revision": self.backend.model_revision,
                        "prompt_version": self.prompt_bank.version,
                        "prompt_hash": self.prompt_bank.prompt_hash,
                    },
                )
            except PersistenceError as exc:
                LOGGER.warning("artwork_filter.prompt_cache_write_failed error=%s", exc)
        return embeddings

    def _aggregate_class_logits(self, row: np.ndarray) -> np.ndarray:
        values = []
        for content_class in PROMPTED_CLASSES:
            indexes = [
                index
                for index, membership in enumerate(self._memberships)
                if membership is content_class
            ]
            class_values = np.sort(row[indexes])
            top_k = min(self.prompt_bank.top_k, len(class_values))
            values.append(float(np.mean(class_values[-top_k:])))
        return np.asarray(values, dtype=np.float64)


def _group_values(class_logits: np.ndarray, classes: Sequence[ContentClass]) -> np.ndarray:
    indexes = [PROMPTED_CLASSES.index(content_class) for content_class in classes]
    return class_logits[indexes]


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - float(np.max(values))
    exponentials = np.exp(shifted)
    return exponentials / float(np.sum(exponentials))


def _logsumexp(values: np.ndarray) -> float:
    maximum = float(np.max(values))
    return maximum + log(sum(exp(float(value) - maximum) for value in values))


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + exp(-value))
    exponential = exp(value)
    return exponential / (1.0 + exponential)
