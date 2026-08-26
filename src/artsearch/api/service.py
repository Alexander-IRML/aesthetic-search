from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
import logging
import time
import uuid

from PIL import Image

from artsearch.api.catalog import ArtworkCatalog
from artsearch.api.config import SearchApiConfig
from artsearch.api.encoder import QueryEncoder
from artsearch.api.errors import (
    CatalogConsistencyError,
    FeedbackTargetNotFoundError,
    FeedbackUnavailableError,
    SearchBusyError,
    SearchTimeoutError,
)
from artsearch.api.schemas import (
    ArtworkSearchResult,
    ImageSearchResponse,
    MatchEvidence,
    ModelContract,
    SearchTiming,
)
from artsearch.api.telemetry import (
    FeedbackTelemetryReceipt,
    NoopSearchTelemetry,
    QueryTelemetryContext,
    SearchTelemetry,
)
from artsearch.ingest.config import ModelConfig
from artsearch.production.config import QdrantConfig
from artsearch.retrieval.qdrant import (
    QdrantSearchHit,
    QdrantSearchService,
    model_bundle_version,
)


LOGGER = logging.getLogger(__name__)


class ImageSearchService:
    """Own one bounded synchronous CLIP/CSD query pipeline behind an async API."""

    def __init__(
        self,
        *,
        api_config: SearchApiConfig,
        model_config: ModelConfig,
        qdrant_config: QdrantConfig,
        encoder: QueryEncoder,
        search: QdrantSearchService,
        catalog: ArtworkCatalog,
        telemetry: SearchTelemetry,
        close_callbacks: Sequence[Callable[[], None]] = (),
    ) -> None:
        self.api_config = api_config
        self.model_config = model_config
        self.qdrant_config = qdrant_config
        self.encoder = encoder
        self.search_backend = search
        self.catalog = catalog
        self.telemetry = telemetry
        self._close_callbacks = tuple(close_callbacks)
        self.model_bundle = model_bundle_version(model_config, qdrant_config)
        self.index_version = qdrant_config.collection_name
        self._inference_slots = asyncio.Semaphore(api_config.max_concurrent_inferences)
        self._executor = ThreadPoolExecutor(
            max_workers=api_config.max_concurrent_inferences,
            thread_name_prefix="artsearch-query-pipeline",
        )
        self.ready = True

    async def search_image(
        self,
        image: Image.Image,
        *,
        client_session_key: str,
        upload_decode_ms: float,
    ) -> ImageSearchResponse:
        """Search one image and transfer ownership of it to the inference worker."""
        queue_started = time.perf_counter()
        try:
            await asyncio.wait_for(
                self._inference_slots.acquire(),
                timeout=self.api_config.inference_queue_timeout_seconds,
            )
        except TimeoutError as exc:
            raise SearchBusyError("Image encoding is busy. Try again shortly.") from exc
        queue_ms = _elapsed_ms(queue_started)

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self._executor,
            self._search_sync,
            image,
            client_session_key,
            max(upload_decode_ms, 0.0),
            queue_ms,
        )
        release_in_callback = False
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self.api_config.inference_timeout_seconds,
            )
        except TimeoutError as exc:
            release_in_callback = True
            future.add_done_callback(lambda _: self._inference_slots.release())
            raise SearchTimeoutError("Image encoding exceeded its time limit.") from exc
        except asyncio.CancelledError:
            release_in_callback = True
            future.add_done_callback(lambda _: self._inference_slots.release())
            raise
        finally:
            if not release_in_callback:
                self._inference_slots.release()

    def _search_sync(
        self,
        image: Image.Image,
        client_session_key: str,
        upload_decode_ms: float,
        queue_ms: float,
    ) -> ImageSearchResponse:
        started = time.perf_counter()
        context = self._start_telemetry(client_session_key)
        timings: dict[str, float] = {
            "upload_decode_ms": upload_decode_ms,
            "queue_ms": queue_ms,
            "encode_ms": 0.0,
            "vector_search_ms": 0.0,
            "catalog_ms": 0.0,
        }
        try:
            encode_started = time.perf_counter()
            embeddings = self.encoder.encode(image)
            timings["encode_ms"] = _elapsed_ms(encode_started)

            search_started = time.perf_counter()
            candidate_depth = min(
                self.api_config.candidate_telemetry_limit,
                self.qdrant_config.fusion_limit,
            )
            hits = self.search_backend.search_by_vectors(
                embeddings.clip_vector,
                embeddings.csd_style,
                top_k=candidate_depth,
            )
            timings["vector_search_ms"] = _elapsed_ms(search_started)

            displayed_hits = hits[: self.api_config.result_limit]
            catalog_started = time.perf_counter()
            records = self.catalog.get_many([hit.artwork_id for hit in displayed_hits])
            timings["catalog_ms"] = _elapsed_ms(catalog_started)
            missing = [hit.artwork_id for hit in displayed_hits if hit.artwork_id not in records]
            unusable = [
                hit.artwork_id
                for hit in displayed_hits
                if hit.artwork_id in records and not records[hit.artwork_id].image_url
            ]
            if missing or unusable:
                raise CatalogConsistencyError(
                    "The active index contains results without public catalog images."
                )

            results = [
                ArtworkSearchResult(
                    artwork_id=hit.artwork_id,
                    artist_id=hit.artist_id,
                    artist_name=records[hit.artwork_id].artist_name,
                    rank=hit.rank,
                    image_url=records[hit.artwork_id].image_url,
                    source_url=records[hit.artwork_id].source_url,
                    content_class=hit.content_class,
                    match=MatchEvidence(
                        fusion_score=hit.fusion_score,
                        subject_score=hit.clip_score,
                        subject_rank=hit.clip_rank,
                        style_score=hit.style_score,
                        style_rank=hit.style_rank,
                    ),
                )
                for hit in displayed_hits
            ]
            timings["total_ms"] = upload_decode_ms + _elapsed_ms(started)
            self._complete_telemetry(
                context,
                hits,
                timings,
                displayed_count=len(displayed_hits),
            )
            return ImageSearchResponse(
                api_version=self.api_config.version,
                query_id=str(context.query_id),
                session_id=str(context.session_id),
                result_count=len(results),
                results=results,
                model=ModelContract(
                    bundle_version=self.model_bundle,
                    index_alias=self.qdrant_config.alias_name,
                    clip_model=self.model_config.clip_model_name,
                    clip_revision=self.model_config.clip_model_version,
                    csd_model=self.model_config.csd_model_name,
                    csd_revision=self.model_config.csd_model_version,
                ),
                timing=SearchTiming(**timings),
            )
        except Exception as exc:
            timings["total_ms"] = upload_decode_ms + _elapsed_ms(started)
            self._fail_telemetry(context, exc, timings)
            raise
        finally:
            image.close()

    async def submit_feedback(
        self,
        *,
        client_session_key: str,
        query_id: uuid.UUID,
        artwork_id: str,
        grade: str,
    ) -> FeedbackTelemetryReceipt:
        if not self.telemetry.feedback_enabled:
            raise FeedbackUnavailableError("Result feedback is not configured.")
        try:
            receipt = await asyncio.to_thread(
                self.telemetry.feedback,
                client_session_key=client_session_key,
                query_id=query_id,
                artwork_id=artwork_id,
                grade=grade,
            )
        except Exception as exc:
            LOGGER.warning("search feedback recording failed: %s", type(exc).__name__)
            raise FeedbackUnavailableError(
                "Result feedback could not be recorded right now."
            ) from exc
        if receipt is None:
            raise FeedbackTargetNotFoundError(
                "This result was not shown by the specified search session."
            )
        return receipt

    def _start_telemetry(self, client_session_key: str) -> QueryTelemetryContext:
        try:
            return self.telemetry.start(
                client_session_key=client_session_key,
                model_bundle_version=self.model_bundle,
                index_version=self.index_version,
            )
        except Exception as exc:
            LOGGER.warning("search telemetry start failed: %s", type(exc).__name__)
            return NoopSearchTelemetry().start(
                client_session_key=client_session_key,
                model_bundle_version=self.model_bundle,
                index_version=self.index_version,
            )

    def _complete_telemetry(
        self,
        context: QueryTelemetryContext,
        hits: Sequence[QdrantSearchHit],
        timings: dict[str, float],
        *,
        displayed_count: int,
    ) -> None:
        try:
            self.telemetry.complete(
                context,
                hits=hits,
                model_bundle_version=self.model_bundle,
                index_version=self.index_version,
                timings=timings,
                displayed_count=displayed_count,
            )
        except Exception as exc:
            LOGGER.warning("search telemetry completion failed: %s", type(exc).__name__)

    def _fail_telemetry(
        self,
        context: QueryTelemetryContext,
        error: Exception,
        timings: dict[str, float],
    ) -> None:
        try:
            self.telemetry.fail(
                context,
                error_type=type(error).__name__,
                latency_ms=timings["total_ms"],
                timings=timings,
            )
        except Exception as exc:
            LOGGER.warning("search telemetry failure recording failed: %s", type(exc).__name__)

    def close(self) -> None:
        self.ready = False
        self._executor.shutdown(wait=False, cancel_futures=True)
        for callback in self._close_callbacks:
            callback()


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0
