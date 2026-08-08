from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import logging
from time import perf_counter
from typing import Protocol

import numpy as np
from PIL import Image

from artsearch.artwork_filter.config import ArtworkFilterConfig
from artsearch.artwork_filter.ensemble import DecisionEngine
from artsearch.artwork_filter.errors import (
    ArtworkFilterError,
    ImageValidationError,
    PersistenceError,
)
from artsearch.artwork_filter.feature_store import FeatureStore, image_feature_cache_key
from artsearch.artwork_filter.image_io import HttpOrLocalImageLoader, ImageLoader
from artsearch.artwork_filter.persistence import DecisionStore
from artsearch.artwork_filter.rules import evaluate_rules
from artsearch.artwork_filter.enums import RuleDisposition
from artsearch.artwork_filter.schemas import (
    FilterResult,
    ImageCandidate,
    LoadedImage,
    RuleResult,
    TextScores,
    VisualScores,
)
from artsearch.artwork_filter.text_signals import score_text


LOGGER = logging.getLogger(__name__)


class VisualClassifier(Protocol):
    @property
    def prompt_version(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def model_revision(self) -> str | None: ...

    @property
    def embedding_dimension(self) -> int: ...

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray: ...

    def classify_embeddings(
        self,
        image_embeddings: np.ndarray,
        *,
        cache_keys: Sequence[str | None] | None = None,
    ) -> list[VisualScores]: ...


@dataclass(frozen=True)
class _PreparedCandidate:
    index: int
    candidate: ImageCandidate
    loaded_image: LoadedImage
    rule_result: RuleResult
    text_scores: TextScores


class ArtworkFilterService:
    def __init__(
        self,
        config: ArtworkFilterConfig,
        image_loader: ImageLoader | None = None,
        visual_classifier: VisualClassifier | None = None,
        feature_store: FeatureStore | None = None,
        decision_store: DecisionStore | None = None,
        decision_engine: DecisionEngine | None = None,
    ) -> None:
        self.config = config
        self._owns_image_loader = image_loader is None
        self.image_loader = image_loader or HttpOrLocalImageLoader(config)
        self.visual_classifier = visual_classifier
        self.feature_store = feature_store
        self.decision_store = decision_store
        self.decision_engine = decision_engine or DecisionEngine(config)

    async def __aenter__(self) -> "ArtworkFilterService":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._owns_image_loader:
            return
        close = getattr(self.image_loader, "aclose", None)
        if close is not None:
            await close()

    async def classify(self, candidate: ImageCandidate) -> FilterResult:
        return (await self.classify_many([candidate]))[0]

    async def classify_many(self, candidates: Sequence[ImageCandidate]) -> list[FilterResult]:
        if not candidates:
            return []

        starts = [perf_counter() for _ in candidates]
        loaded = await self._load_many(candidates)
        try:
            return self._classify_loaded(candidates, loaded, starts)
        finally:
            for loaded_or_error in loaded:
                if isinstance(loaded_or_error, LoadedImage):
                    loaded_or_error.rgb_image.close()

    def _classify_loaded(
        self,
        candidates: Sequence[ImageCandidate],
        loaded: list[LoadedImage | Exception],
        starts: list[float],
    ) -> list[FilterResult]:
        results: list[FilterResult | None] = [None] * len(candidates)
        prepared: list[_PreparedCandidate] = []

        for index, (candidate, loaded_or_error) in enumerate(zip(candidates, loaded, strict=True)):
            if isinstance(loaded_or_error, Exception):
                results[index] = self.decision_engine.error_result(
                    candidate,
                    loaded_or_error,
                    duration_ms=_elapsed_ms(starts[index]),
                )
                continue

            rule_result = evaluate_rules(candidate, loaded_or_error, self.config)
            text_scores = score_text(candidate)
            item = _PreparedCandidate(
                index=index,
                candidate=candidate,
                loaded_image=loaded_or_error,
                rule_result=rule_result,
                text_scores=text_scores,
            )
            if rule_result.disposition == RuleDisposition.FORCE_REJECT:
                results[index] = self._decide(item, None, starts[index])
            else:
                prepared.append(item)

        if prepared:
            if self.visual_classifier is None:
                for item in prepared:
                    results[item.index] = self._decide(item, None, starts[item.index])
            else:
                self._classify_prepared(prepared, results, starts)

        finalized = [result for result in results if result is not None]
        if len(finalized) != len(candidates):
            raise RuntimeError("artwork filter did not produce one result per candidate")
        finalized = self._persist(finalized, candidates, starts)
        return finalized

    async def _load_many(
        self,
        candidates: Sequence[ImageCandidate],
    ) -> list[LoadedImage | Exception]:
        semaphore = asyncio.Semaphore(self.config.downloads.max_concurrency)

        async def load_one(candidate: ImageCandidate) -> LoadedImage | Exception:
            async with semaphore:
                try:
                    return await self.image_loader.load(candidate)
                except (ArtworkFilterError, OSError, ValueError) as exc:
                    return exc
                except Exception as exc:
                    return ImageValidationError(f"unexpected image-load failure: {exc}")

        return list(await asyncio.gather(*(load_one(candidate) for candidate in candidates)))

    def _classify_prepared(
        self,
        prepared: list[_PreparedCandidate],
        results: list[FilterResult | None],
        starts: list[float],
    ) -> None:
        assert self.visual_classifier is not None
        try:
            embeddings, cache_keys = self._embeddings_for(prepared)
            visual_scores = self.visual_classifier.classify_embeddings(
                embeddings,
                cache_keys=cache_keys,
            )
            if len(visual_scores) != len(prepared):
                raise ValueError("visual classifier returned the wrong result count")
        except (ArtworkFilterError, RuntimeError, ValueError, TypeError) as exc:
            for item in prepared:
                results[item.index] = self.decision_engine.error_result(
                    item.candidate,
                    exc,
                    duration_ms=_elapsed_ms(starts[item.index]),
                )
            return

        for item, visual in zip(prepared, visual_scores, strict=True):
            results[item.index] = self._decide(item, visual, starts[item.index])

    def _embeddings_for(
        self,
        prepared: list[_PreparedCandidate],
    ) -> tuple[np.ndarray, list[str | None]]:
        assert self.visual_classifier is not None
        expected_dimension = self.visual_classifier.embedding_dimension
        vectors: list[np.ndarray | None] = [None] * len(prepared)
        cache_keys: list[str | None] = [None] * len(prepared)
        missing_indexes = []

        for index, item in enumerate(prepared):
            cache_key = image_feature_cache_key(
                image_sha256=item.loaded_image.sha256,
                model_id=self.visual_classifier.model_id,
                model_revision=self.visual_classifier.model_revision,
                preprocessing_version=self.config.model.preprocessing_version,
                normalize_embeddings=self.config.model.normalize_embeddings,
            )
            cache_keys[index] = cache_key
            cached = (
                self.feature_store.get(cache_key)
                if self.feature_store is not None and self.config.model.cache_embeddings
                else None
            )
            if cached is not None and cached.shape == (expected_dimension,):
                vectors[index] = cached
            else:
                missing_indexes.append(index)

        if missing_indexes:
            missing_images = [prepared[index].loaded_image.rgb_image for index in missing_indexes]
            computed = self.visual_classifier.encode_images(missing_images)
            if computed.shape != (len(missing_indexes), expected_dimension):
                raise ValueError("visual backend returned an unexpected image-embedding shape")
            for row, index in enumerate(missing_indexes):
                vector = np.asarray(computed[row], dtype=np.float32)
                vectors[index] = vector
                if self.feature_store is not None and self.config.model.cache_embeddings:
                    try:
                        self.feature_store.put(
                            cache_keys[index] or "",
                            vector,
                            {
                                "kind": "image_embedding",
                                "image_sha256": prepared[index].loaded_image.sha256,
                                "model_id": self.visual_classifier.model_id,
                                "model_revision": self.visual_classifier.model_revision,
                                "preprocessing_version": self.config.model.preprocessing_version,
                            },
                        )
                    except PersistenceError as exc:
                        LOGGER.warning("artwork_filter.feature_cache_write_failed error=%s", exc)

        if any(vector is None for vector in vectors):
            raise RuntimeError("missing image embedding after cache/inference pass")
        return np.stack([vector for vector in vectors if vector is not None]), cache_keys

    def _decide(
        self,
        item: _PreparedCandidate,
        visual_scores: VisualScores | None,
        start: float,
    ) -> FilterResult:
        prompt_version = (
            self.visual_classifier.prompt_version if self.visual_classifier is not None else None
        )
        return self.decision_engine.decide(
            item.candidate,
            item.loaded_image,
            item.rule_result,
            visual_scores,
            item.text_scores,
            duration_ms=_elapsed_ms(start),
            prompt_version=prompt_version,
        )

    def _persist(
        self,
        results: list[FilterResult],
        candidates: Sequence[ImageCandidate],
        starts: list[float],
    ) -> list[FilterResult]:
        if self.decision_store is None:
            return results
        try:
            self.decision_store.append_many(results)
            return results
        except PersistenceError as exc:
            if self.config.storage.durability_mode == "best_effort":
                LOGGER.error("artwork_filter.decision_persistence_failed error=%s", exc)
                return results
            return [
                self.decision_engine.error_result(
                    candidate,
                    exc,
                    duration_ms=_elapsed_ms(start),
                )
                for candidate, start in zip(candidates, starts, strict=True)
            ]


def _elapsed_ms(start: float) -> float:
    return (perf_counter() - start) * 1000
