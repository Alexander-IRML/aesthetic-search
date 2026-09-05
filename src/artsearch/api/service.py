from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
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
    ResultSourceMetadata,
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


@dataclass(frozen=True)
class _SearchExecution:
    """Carry telemetry evidence across the worker/event-loop boundary."""

    context: QueryTelemetryContext
    timings: dict[str, float]
    response: ImageSearchResponse | None = None
    hits: tuple[QdrantSearchHit, ...] = ()
    displayed_count: int = 0
    error: Exception | None = None


@dataclass(frozen=True)
class _TelemetryStart:
    context: QueryTelemetryContext
    duration_ms: float


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
        self._telemetry_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="artsearch-query-telemetry",
        )
        self.ready = True

    async def search_image(
        self,
        image: Image.Image,
        *,
        client_session_key: str,
        upload_decode_ms: float,
        artist_id: str | None = None,
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
        telemetry_future = self._queue_telemetry_start(
            client_session_key,
            artist_id=artist_id,
        )
        future = self._executor.submit(
            self._search_sync,
            image,
            telemetry_future,
            max(upload_decode_ms, 0.0),
            queue_ms,
            artist_id,
        )
        release_in_callback = False
        try:
            deadline = loop.time() + self.api_config.inference_timeout_seconds
            while not future.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    release_in_callback = True
                    timeout_error = SearchTimeoutError("Image encoding exceeded its time limit.")
                    self._finish_later(loop, future, timeout_error)
                    raise timeout_error
                await asyncio.sleep(min(0.01, remaining))
            execution = future.result()
        except asyncio.CancelledError:
            release_in_callback = True
            self._finish_later(
                loop,
                future,
                RuntimeError("search request was cancelled"),
            )
            raise
        finally:
            if not release_in_callback:
                self._inference_slots.release()

        return self._finish_search(execution)

    def _search_sync(
        self,
        image: Image.Image,
        telemetry_future: Future[_TelemetryStart],
        upload_decode_ms: float,
        queue_ms: float,
        artist_id: str | None,
    ) -> _SearchExecution:
        started = time.perf_counter()
        timings: dict[str, float] = {
            "upload_decode_ms": upload_decode_ms,
            "queue_ms": queue_ms,
            "telemetry_start_ms": 0.0,
            "telemetry_wait_ms": 0.0,
            "encode_ms": 0.0,
            "clip_encode_ms": 0.0,
            "csd_encode_ms": 0.0,
            "csd_preprocess_ms": 0.0,
            "csd_tensor_transfer_ms": 0.0,
            "csd_backbone_ms": 0.0,
            "csd_projection_ms": 0.0,
            "csd_output_ms": 0.0,
            "embedding_normalize_ms": 0.0,
            "vector_search_ms": 0.0,
            "catalog_ms": 0.0,
        }
        results: list[ArtworkSearchResult] = []
        hits: Sequence[QdrantSearchHit] = ()
        displayed_count = 0
        error: Exception | None = None
        try:
            encode_started = time.perf_counter()
            encoding = self.encoder.encode(image)
            timings["encode_ms"] = _elapsed_ms(encode_started)
            timings["clip_encode_ms"] = encoding.clip_ms
            timings["csd_encode_ms"] = encoding.csd_ms
            timings["csd_preprocess_ms"] = encoding.csd_preprocess_ms
            timings["csd_tensor_transfer_ms"] = encoding.csd_tensor_transfer_ms
            timings["csd_backbone_ms"] = encoding.csd_backbone_ms
            timings["csd_projection_ms"] = encoding.csd_projection_ms
            timings["csd_output_ms"] = encoding.csd_output_ms
            timings["embedding_normalize_ms"] = encoding.normalize_ms
            embeddings = encoding.embeddings

            search_started = time.perf_counter()
            candidate_depth = min(
                self.api_config.candidate_telemetry_limit,
                self.qdrant_config.fusion_limit,
            )
            hits = self.search_backend.search_by_vectors(
                embeddings.clip_vector,
                embeddings.csd_style,
                top_k=candidate_depth,
                artist_id=artist_id,
            )
            timings["vector_search_ms"] = _elapsed_ms(search_started)

            displayed_hits = hits[: self.api_config.result_limit]
            displayed_count = len(displayed_hits)
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
                    source_metadata=ResultSourceMetadata(
                        post_text=records[hit.artwork_id].post_text,
                        alt_text=records[hit.artwork_id].alt_text,
                    ),
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
        except Exception as exc:
            error = exc
        finally:
            image.close()

        telemetry_wait_started = time.perf_counter()
        telemetry_start = telemetry_future.result()
        timings["telemetry_wait_ms"] = _elapsed_ms(telemetry_wait_started)
        timings["telemetry_start_ms"] = telemetry_start.duration_ms
        timings["total_ms"] = upload_decode_ms + _elapsed_ms(started)
        context = telemetry_start.context

        if error is not None:
            return _SearchExecution(
                context=context,
                timings=timings,
                error=error,
            )

        try:
            response = ImageSearchResponse(
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
            return _SearchExecution(
                context=context,
                timings=timings,
                response=response,
                hits=tuple(hits),
                displayed_count=displayed_count,
            )
        except Exception as exc:
            return _SearchExecution(
                context=context,
                timings=timings,
                error=exc,
            )

    def _finish_search(self, execution: _SearchExecution) -> ImageSearchResponse:
        if execution.error is not None:
            self._queue_telemetry_failure(
                execution.context,
                execution.error,
                execution.timings,
            )
            raise execution.error
        if execution.response is None:
            raise RuntimeError("search worker returned no response")
        self._queue_telemetry_completion(
            execution.context,
            execution.hits,
            execution.timings,
            displayed_count=execution.displayed_count,
        )
        return execution.response

    def _finish_later(
        self,
        loop: asyncio.AbstractEventLoop,
        future: Future[_SearchExecution],
        error: Exception,
    ) -> None:
        def finish(completed: Future[_SearchExecution]) -> None:
            try:
                loop.call_soon_threadsafe(
                    self._finish_abandoned_search,
                    completed,
                    error,
                )
            except RuntimeError:
                LOGGER.warning("search loop closed before abandoned work completed")

        future.add_done_callback(finish)

    def _finish_abandoned_search(
        self,
        future: Future[_SearchExecution],
        error: Exception,
    ) -> None:
        self._inference_slots.release()
        try:
            execution = future.result()
        except Exception as exc:
            LOGGER.warning("abandoned search worker failed: %s", type(exc).__name__)
            return
        self._queue_telemetry_failure(execution.context, error, execution.timings)

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
            future = self._telemetry_executor.submit(
                partial(
                    self.telemetry.feedback,
                    client_session_key=client_session_key,
                    query_id=query_id,
                    artwork_id=artwork_id,
                    grade=grade,
                ),
            )
            deadline = asyncio.get_running_loop().time() + 5.0
            while not future.done():
                if asyncio.get_running_loop().time() >= deadline:
                    future.cancel()
                    raise TimeoutError
                await asyncio.sleep(0.025)
            receipt = future.result()
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

    def _start_telemetry(
        self,
        client_session_key: str,
        *,
        artist_id: str | None,
    ) -> QueryTelemetryContext:
        try:
            return self.telemetry.start(
                client_session_key=client_session_key,
                model_bundle_version=self.model_bundle,
                index_version=self.index_version,
                artist_id=artist_id,
            )
        except Exception as exc:
            LOGGER.warning("search telemetry start failed: %s", type(exc).__name__)
            return NoopSearchTelemetry().start(
                client_session_key=client_session_key,
                model_bundle_version=self.model_bundle,
                index_version=self.index_version,
                artist_id=artist_id,
            )

    def _start_telemetry_timed(
        self,
        client_session_key: str,
        *,
        artist_id: str | None,
    ) -> _TelemetryStart:
        started = time.perf_counter()
        context = self._start_telemetry(client_session_key, artist_id=artist_id)
        return _TelemetryStart(context=context, duration_ms=_elapsed_ms(started))

    def _queue_telemetry_start(
        self,
        client_session_key: str,
        *,
        artist_id: str | None,
    ) -> Future[_TelemetryStart]:
        if not self.telemetry.feedback_enabled:
            return _completed_future(
                self._start_telemetry_timed(client_session_key, artist_id=artist_id)
            )
        try:
            return self._telemetry_executor.submit(
                self._start_telemetry_timed,
                client_session_key,
                artist_id=artist_id,
            )
        except RuntimeError:
            LOGGER.warning("search telemetry executor is unavailable")
            return _completed_future(
                self._start_telemetry_timed(client_session_key, artist_id=artist_id)
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

    def _queue_telemetry_completion(
        self,
        context: QueryTelemetryContext,
        hits: Sequence[QdrantSearchHit],
        timings: dict[str, float],
        *,
        displayed_count: int,
    ) -> None:
        if not self.telemetry.feedback_enabled:
            return
        try:
            self._telemetry_executor.submit(
                self._complete_telemetry,
                context,
                tuple(hits),
                dict(timings),
                displayed_count=displayed_count,
            )
        except RuntimeError:
            LOGGER.warning("search telemetry executor is unavailable")

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

    def _queue_telemetry_failure(
        self,
        context: QueryTelemetryContext,
        error: Exception,
        timings: dict[str, float],
    ) -> None:
        if not self.telemetry.feedback_enabled:
            return
        try:
            self._telemetry_executor.submit(
                self._fail_telemetry,
                context,
                error,
                dict(timings),
            )
        except RuntimeError:
            LOGGER.warning("search telemetry executor is unavailable")

    def close(self) -> None:
        self.ready = False
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._telemetry_executor.shutdown(wait=True, cancel_futures=False)
        for callback in self._close_callbacks:
            callback()


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _completed_future(value: _TelemetryStart) -> Future[_TelemetryStart]:
    future: Future[_TelemetryStart] = Future()
    future.set_result(value)
    return future
