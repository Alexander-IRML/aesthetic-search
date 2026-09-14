from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
import inspect
import logging
import time
from typing import Any
import uuid

import numpy as np
from PIL import Image

from artsearch.api.catalog import ArtworkCatalog
from artsearch.api.config import SearchApiConfig
from artsearch.api.encoder import QueryEncoder, QueryEncoding
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
    RetrievalWeights,
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
from artsearch.retrieval.backend import (
    AsyncVectorSearchBackend,
    RetrievalContract,
    SearchHit,
)
from artsearch.retrieval.qdrant import model_bundle_version


LOGGER = logging.getLogger(__name__)
CloseCallback = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True)
class _TelemetryStart:
    context: QueryTelemetryContext
    duration_ms: float


class ImageSearchService:
    """Coordinate async serving around one bounded CLIP/CSD model owner."""

    def __init__(
        self,
        *,
        api_config: SearchApiConfig,
        model_config: ModelConfig,
        qdrant_config: QdrantConfig | None = None,
        retrieval_contract: RetrievalContract | None = None,
        encoder: QueryEncoder,
        search: AsyncVectorSearchBackend,
        catalog: ArtworkCatalog,
        telemetry: SearchTelemetry,
        close_callbacks: Sequence[CloseCallback] = (),
    ) -> None:
        self.api_config = api_config
        self.model_config = model_config
        if retrieval_contract is None:
            if qdrant_config is None:
                raise ValueError("a retrieval contract or Qdrant configuration is required")
            retrieval_contract = RetrievalContract(
                backend="qdrant",
                model_bundle_version=model_bundle_version(model_config, qdrant_config),
                index_alias=qdrant_config.alias_name,
                index_version=qdrant_config.collection_name,
                candidate_limit=qdrant_config.fusion_limit,
                clip_dimension=qdrant_config.clip_dimension,
                csd_dimension=qdrant_config.csd_dimension,
            )
        self.retrieval_contract = retrieval_contract
        self.encoder = encoder
        self.search_backend = search
        self.catalog = catalog
        self.telemetry = telemetry
        self._close_callbacks = tuple(close_callbacks)
        self.model_bundle = retrieval_contract.model_bundle_version
        self.index_version = retrieval_contract.index_version

        self._inference_slots = asyncio.Semaphore(api_config.max_concurrent_inferences)
        self._pending_lock = asyncio.Lock()
        self._pending_inferences = 0
        self._active_inferences = 0
        self._executor = ThreadPoolExecutor(
            max_workers=api_config.max_concurrent_inferences,
            thread_name_prefix="artsearch-query-encoder",
        )

        self._telemetry_tasks: set[asyncio.Task[Any]] = set()
        # A feedback request only needs to wait for its own impressions to be
        # committed. Unrelated searches remain fully concurrent.
        self._query_completion_tasks: dict[uuid.UUID, asyncio.Task[Any]] = {}
        self.ready = True
        self._closed = False

    @property
    def inference_status(self) -> dict[str, int]:
        return {
            "active": self._active_inferences,
            "pending": self._pending_inferences,
            "capacity": self.api_config.max_concurrent_inferences,
            "queue_capacity": self.api_config.max_pending_inferences,
        }

    async def search_image(
        self,
        image: Image.Image,
        *,
        client_session_key: str,
        upload_decode_ms: float,
        artist_id: str | None = None,
        retrieval_weights: RetrievalWeights | None = None,
        visitor_hash: str | None = None,
    ) -> ImageSearchResponse:
        """Encode in the model worker, then await retrieval and catalog I/O."""

        started = time.perf_counter()
        timings = _empty_timings(upload_decode_ms)
        weights = retrieval_weights or RetrievalWeights()
        telemetry_task = self._queue_telemetry_start(
            client_session_key,
            artist_id=artist_id,
            retrieval_weights=weights,
            visitor_hash=visitor_hash,
        )

        try:
            encoding, queue_ms, encode_ms = await self._encode_image(image)
            timings["queue_ms"] = queue_ms
            _record_encoding_timings(timings, encoding, encode_ms=encode_ms)

            candidate_depth = min(
                self.api_config.candidate_telemetry_limit,
                self.retrieval_contract.candidate_limit,
            )
            search_started = time.perf_counter()
            clip_vector = _scale_embedding_halves(
                encoding.embeddings.clip_vector,
                first_weight=weights.clip_first_half,
                second_weight=weights.clip_second_half,
                name="CLIP",
            )
            csd_vector = _scale_embedding_halves(
                encoding.embeddings.csd_style,
                first_weight=weights.csd_first_half,
                second_weight=weights.csd_second_half,
                name="CSD",
            )
            hits = await self.search_backend.search_by_vectors(
                clip_vector,
                csd_vector,
                top_k=candidate_depth,
                artist_id=artist_id,
            )
            timings["vector_search_ms"] = _elapsed_ms(search_started)

            displayed_hits = hits[: self.api_config.result_limit]
            catalog_started = time.perf_counter()
            records = await self.catalog.get_many([hit.artwork_id for hit in displayed_hits])
            timings["catalog_ms"] = _elapsed_ms(catalog_started)
            _validate_catalog(displayed_hits, records)

            telemetry_start = await self._await_telemetry_start(telemetry_task, timings)
            timings["total_ms"] = max(upload_decode_ms, 0.0) + _elapsed_ms(started)
            response = self._build_response(
                telemetry_start.context,
                displayed_hits,
                records,
                timings,
                weights,
            )
        except asyncio.CancelledError:
            timings["total_ms"] = max(upload_decode_ms, 0.0) + _elapsed_ms(started)
            self._queue_failure_after_start(
                telemetry_task,
                RuntimeError("search request was cancelled"),
                timings,
            )
            raise
        except Exception as exc:
            timings["total_ms"] = max(upload_decode_ms, 0.0) + _elapsed_ms(started)
            telemetry_start = await self._await_telemetry_start(telemetry_task, timings)
            self._queue_telemetry_failure(telemetry_start.context, exc, timings)
            raise

        self._queue_telemetry_completion(
            telemetry_start.context,
            hits,
            timings,
            displayed_count=len(displayed_hits),
        )
        return response

    async def record_site_visit(
        self,
        *,
        client_session_key: str,
        visitor_hash: str,
    ) -> bool:
        """Persist a pseudonymous visit without putting an IP on the event loop."""

        if not self.telemetry.feedback_enabled:
            return False
        try:
            return await asyncio.wait_for(
                self.telemetry.record_visit(
                    client_session_key=client_session_key,
                    visitor_hash=visitor_hash,
                ),
                timeout=5.0,
            )
        except Exception as exc:
            LOGGER.warning("site visit recording failed: %s", type(exc).__name__)
            return False

    async def _encode_image(
        self,
        image: Image.Image,
    ) -> tuple[QueryEncoding, float, float]:
        queue_started = time.perf_counter()
        pending = False
        acquired = False
        future: Future[QueryEncoding] | None = None
        loop: asyncio.AbstractEventLoop | None = None
        release_later = False
        try:
            async with self._pending_lock:
                if self._pending_inferences >= self.api_config.max_pending_inferences:
                    raise SearchBusyError("Image encoding queue is full. Try again shortly.")
                self._pending_inferences += 1
                pending = True
            try:
                await asyncio.wait_for(
                    self._inference_slots.acquire(),
                    timeout=self.api_config.inference_queue_timeout_seconds,
                )
                acquired = True
            except TimeoutError as exc:
                raise SearchBusyError("Image encoding is busy. Try again shortly.") from exc
            finally:
                if pending:
                    async with self._pending_lock:
                        self._pending_inferences -= 1
                    pending = False

            queue_ms = _elapsed_ms(queue_started)
            self._active_inferences += 1
            loop = asyncio.get_running_loop()
            encode_started = time.perf_counter()
            future = self._executor.submit(self._encode_sync, image)

            wrapped = asyncio.wrap_future(future)
            encoding = await asyncio.wait_for(
                asyncio.shield(wrapped),
                timeout=self.api_config.inference_timeout_seconds,
            )
            return encoding, queue_ms, _elapsed_ms(encode_started)
        except TimeoutError as exc:
            if loop is None or future is None:
                raise
            release_later = True
            self._release_inference_later(loop, future)
            raise SearchTimeoutError("Image encoding exceeded its time limit.") from exc
        except asyncio.CancelledError:
            if future is not None:
                release_later = True
                self._release_inference_later(loop, future)
            raise
        finally:
            if pending:
                async with self._pending_lock:
                    self._pending_inferences -= 1
            if future is None:
                image.close()
            if acquired and not release_later:
                self._release_inference_slot()

    def _encode_sync(self, image: Image.Image) -> QueryEncoding:
        try:
            return self.encoder.encode(image)
        finally:
            image.close()

    def _release_inference_later(
        self,
        loop: asyncio.AbstractEventLoop,
        future: Future[QueryEncoding],
    ) -> None:
        def release(_completed: Future[QueryEncoding]) -> None:
            try:
                loop.call_soon_threadsafe(self._release_inference_slot)
            except RuntimeError:
                LOGGER.warning("search loop closed before model inference completed")

        future.add_done_callback(release)

    def _release_inference_slot(self) -> None:
        self._active_inferences -= 1
        self._inference_slots.release()

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

        async def record_after_completion() -> FeedbackTelemetryReceipt | None:
            completion = self._query_completion_tasks.get(query_id)
            if completion is not None:
                await asyncio.shield(completion)
            return await self.telemetry.feedback(
                client_session_key=client_session_key,
                query_id=query_id,
                artwork_id=artwork_id,
                grade=grade,
            )

        task = self._create_telemetry_task(record_after_completion())
        try:
            receipt = await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
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

    async def _start_telemetry(
        self,
        client_session_key: str,
        *,
        artist_id: str | None,
        retrieval_weights: RetrievalWeights,
        visitor_hash: str | None,
    ) -> QueryTelemetryContext:
        try:
            return await self.telemetry.start(
                client_session_key=client_session_key,
                model_bundle_version=self.model_bundle,
                index_version=self.index_version,
                vector_backend=self.retrieval_contract.backend,
                artist_id=artist_id,
                retrieval_weights=retrieval_weights.model_dump(),
                visitor_hash=visitor_hash,
            )
        except Exception as exc:
            LOGGER.warning("search telemetry start failed: %s", type(exc).__name__)
            return await NoopSearchTelemetry().start(
                client_session_key=client_session_key,
                model_bundle_version=self.model_bundle,
                index_version=self.index_version,
                vector_backend=self.retrieval_contract.backend,
                artist_id=artist_id,
                retrieval_weights=retrieval_weights.model_dump(),
                visitor_hash=visitor_hash,
            )

    async def _start_telemetry_timed(
        self,
        client_session_key: str,
        *,
        artist_id: str | None,
        retrieval_weights: RetrievalWeights,
        visitor_hash: str | None,
    ) -> _TelemetryStart:
        started = time.perf_counter()
        context = await self._start_telemetry(
            client_session_key,
            artist_id=artist_id,
            retrieval_weights=retrieval_weights,
            visitor_hash=visitor_hash,
        )
        return _TelemetryStart(context=context, duration_ms=_elapsed_ms(started))

    def _queue_telemetry_start(
        self,
        client_session_key: str,
        *,
        artist_id: str | None,
        retrieval_weights: RetrievalWeights,
        visitor_hash: str | None,
    ) -> asyncio.Task[_TelemetryStart]:
        return self._create_telemetry_task(
            self._start_telemetry_timed(
                client_session_key,
                artist_id=artist_id,
                retrieval_weights=retrieval_weights,
                visitor_hash=visitor_hash,
            )
        )

    async def _await_telemetry_start(
        self,
        task: asyncio.Task[_TelemetryStart],
        timings: dict[str, float],
    ) -> _TelemetryStart:
        wait_started = time.perf_counter()
        telemetry_start = await asyncio.shield(task)
        timings["telemetry_wait_ms"] = _elapsed_ms(wait_started)
        timings["telemetry_start_ms"] = telemetry_start.duration_ms
        return telemetry_start

    async def _complete_telemetry(
        self,
        context: QueryTelemetryContext,
        hits: Sequence[SearchHit],
        timings: dict[str, float],
        *,
        displayed_count: int,
    ) -> None:
        try:
            await self.telemetry.complete(
                context,
                hits=hits,
                model_bundle_version=self.model_bundle,
                index_version=self.index_version,
                vector_backend=self.retrieval_contract.backend,
                timings=timings,
                displayed_count=displayed_count,
            )
        except Exception as exc:
            LOGGER.warning("search telemetry completion failed: %s", type(exc).__name__)

    def _queue_telemetry_completion(
        self,
        context: QueryTelemetryContext,
        hits: Sequence[SearchHit],
        timings: dict[str, float],
        *,
        displayed_count: int,
    ) -> None:
        if not self.telemetry.feedback_enabled:
            return
        task = self._create_telemetry_task(
            self._complete_telemetry(
                context,
                tuple(hits),
                dict(timings),
                displayed_count=displayed_count,
            )
        )
        self._query_completion_tasks[context.query_id] = task
        task.add_done_callback(partial(self._forget_query_completion, context.query_id))

    async def _fail_telemetry(
        self,
        context: QueryTelemetryContext,
        error: Exception,
        timings: dict[str, float],
    ) -> None:
        try:
            await self.telemetry.fail(
                context,
                error_type=type(error).__name__,
                latency_ms=timings["total_ms"],
                timings=timings,
            )
        except Exception as exc:
            LOGGER.warning(
                "search telemetry failure recording failed: %s",
                type(exc).__name__,
            )

    def _queue_telemetry_failure(
        self,
        context: QueryTelemetryContext,
        error: Exception,
        timings: dict[str, float],
    ) -> None:
        if not self.telemetry.feedback_enabled:
            return
        self._create_telemetry_task(self._fail_telemetry(context, error, dict(timings)))

    def _queue_failure_after_start(
        self,
        start_task: asyncio.Task[_TelemetryStart],
        error: Exception,
        timings: dict[str, float],
    ) -> None:
        if not self.telemetry.feedback_enabled:
            return

        async def fail_after_start() -> None:
            telemetry_start = await asyncio.shield(start_task)
            await self._fail_telemetry(telemetry_start.context, error, dict(timings))

        self._create_telemetry_task(fail_after_start())

    def _create_telemetry_task(
        self,
        operation: Awaitable[Any],
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(operation)
        self._telemetry_tasks.add(task)
        task.add_done_callback(self._telemetry_task_done)
        return task

    def _forget_query_completion(
        self,
        query_id: uuid.UUID,
        task: asyncio.Task[Any],
    ) -> None:
        if self._query_completion_tasks.get(query_id) is task:
            self._query_completion_tasks.pop(query_id, None)

    def _telemetry_task_done(self, task: asyncio.Task[Any]) -> None:
        self._telemetry_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception as exc:
            LOGGER.warning("telemetry task inspection failed: %s", type(exc).__name__)
            return
        if error is not None:
            LOGGER.warning("telemetry task failed: %s", type(error).__name__)

    def _build_response(
        self,
        context: QueryTelemetryContext,
        displayed_hits: Sequence[SearchHit],
        records: dict[str, Any],
        timings: dict[str, float],
        retrieval_weights: RetrievalWeights,
    ) -> ImageSearchResponse:
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
        return ImageSearchResponse(
            api_version=self.api_config.version,
            query_id=str(context.query_id),
            session_id=str(context.session_id),
            result_count=len(results),
            results=results,
            retrieval_weights=retrieval_weights,
            model=ModelContract(
                bundle_version=self.model_bundle,
                vector_backend=self.retrieval_contract.backend,
                index_alias=self.retrieval_contract.index_alias,
                index_version=self.retrieval_contract.index_version,
                clip_model=self.model_config.clip_model_name,
                clip_revision=self.model_config.clip_model_version,
                csd_model=self.model_config.csd_model_name,
                csd_revision=self.model_config.csd_model_version,
            ),
            timing=SearchTiming(**timings),
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.ready = False
        pending = tuple(task for task in self._telemetry_tasks if not task.done())
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(*pending, return_exceptions=True)),
                    timeout=5.0,
                )
            except (Exception, asyncio.CancelledError):
                LOGGER.warning("telemetry did not drain cleanly during shutdown")
        self._executor.shutdown(wait=False, cancel_futures=True)
        for callback in reversed(self._close_callbacks):
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                LOGGER.warning(
                    "search resource cleanup failed: %s",
                    type(exc).__name__,
                )

    def close(self) -> None:
        """Compatibility entry point for synchronous service owners."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        raise RuntimeError("use 'await service.aclose()' from asynchronous code")


def _empty_timings(upload_decode_ms: float) -> dict[str, float]:
    return {
        "upload_decode_ms": max(upload_decode_ms, 0.0),
        "queue_ms": 0.0,
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
        "total_ms": 0.0,
    }


def _record_encoding_timings(
    timings: dict[str, float],
    encoding: QueryEncoding,
    *,
    encode_ms: float,
) -> None:
    timings["encode_ms"] = encode_ms
    timings["clip_encode_ms"] = encoding.clip_ms
    timings["csd_encode_ms"] = encoding.csd_ms
    timings["csd_preprocess_ms"] = encoding.csd_preprocess_ms
    timings["csd_tensor_transfer_ms"] = encoding.csd_tensor_transfer_ms
    timings["csd_backbone_ms"] = encoding.csd_backbone_ms
    timings["csd_projection_ms"] = encoding.csd_projection_ms
    timings["csd_output_ms"] = encoding.csd_output_ms
    timings["embedding_normalize_ms"] = encoding.normalize_ms


def _scale_embedding_halves(
    vector: np.ndarray,
    *,
    first_weight: float,
    second_weight: float,
    name: str,
) -> np.ndarray:
    """Scale two arbitrary coordinate blocks and restore a unit cosine vector."""

    values = np.asarray(vector, dtype=np.float32)
    if values.ndim != 1 or values.size < 2 or not np.isfinite(values).all():
        raise RuntimeError(f"{name} query embedding is malformed")
    midpoint = values.size // 2
    scaled = values.copy()
    scaled[:midpoint] *= first_weight
    scaled[midpoint:] *= second_weight
    norm = float(np.linalg.norm(scaled))
    if not np.isfinite(norm) or norm <= 0.0:
        raise RuntimeError(f"{name} query embedding cannot be normalized")
    return (scaled / norm).astype(np.float32)


def _validate_catalog(
    hits: Sequence[SearchHit],
    records: dict[str, Any],
) -> None:
    missing = [hit.artwork_id for hit in hits if hit.artwork_id not in records]
    unusable = [
        hit.artwork_id
        for hit in hits
        if hit.artwork_id in records and not records[hit.artwork_id].image_url
    ]
    if missing or unusable:
        raise CatalogConsistencyError(
            "The active index contains results without public catalog images."
        )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0
