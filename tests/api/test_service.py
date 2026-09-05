from __future__ import annotations

import asyncio
from threading import Event
import uuid

import numpy as np
from PIL import Image
import pytest

from artsearch.api.catalog import CatalogArtwork
from artsearch.api.config import SearchApiConfig
from artsearch.api.encoder import QueryEncoding
from artsearch.api.service import ImageSearchService
from artsearch.api.telemetry import (
    FeedbackTelemetryReceipt,
    NoopSearchTelemetry,
    QueryTelemetryContext,
)
from artsearch.embed.storage import ImageEmbeddings
from artsearch.ingest.config import ModelConfig
from artsearch.production.config import QdrantConfig
from artsearch.retrieval.qdrant import QdrantSearchHit


class _Encoder:
    def encode(self, image: Image.Image) -> QueryEncoding:
        assert image.mode == "RGB"
        return QueryEncoding(
            embeddings=ImageEmbeddings(
                clip_vector=np.array([1.0, 0.0], dtype=np.float32),
                csd_style=np.array([0.0, 1.0], dtype=np.float32),
            ),
            clip_ms=1.25,
            csd_ms=7.5,
            normalize_ms=0.25,
            csd_preprocess_ms=0.5,
            csd_tensor_transfer_ms=0.1,
            csd_backbone_ms=6.5,
            csd_projection_ms=0.3,
            csd_output_ms=0.1,
        )


class _Search:
    def __init__(self) -> None:
        self.top_k = 0
        self.artist_id = None

    def search_by_vectors(self, clip, style, *, top_k: int, artist_id=None):
        assert np.asarray(clip).tolist() == [1.0, 0.0]
        assert np.asarray(style).tolist() == [0.0, 1.0]
        self.top_k = top_k
        self.artist_id = artist_id
        return [
            QdrantSearchHit(
                artwork_id=f"art-{index}",
                artist_id=f"artist-{index}",
                content_class="finished_illustration",
                score=0.8,
                rank=index,
                fusion_score=0.8,
                fusion_rank=index,
                clip_score=0.7,
                clip_rank=index,
                style_score=0.6,
                style_rank=index,
            )
            for index in range(1, top_k + 1)
        ]


class _Catalog:
    def get_many(self, artwork_ids):
        return {
            artwork_id: CatalogArtwork(
                artwork_id=artwork_id,
                artist_id=artwork_id.replace("art", "artist"),
                artist_name=f"Artist {index}",
                image_url=f"/api/artworks/{artwork_id}/image",
                post_text=f"Post about artwork {index}",
                alt_text=f"Alt text for artwork {index}",
            )
            for index, artwork_id in enumerate(artwork_ids, start=1)
        }

    def local_image_path(self, artwork_id):
        return None


class _BlockingTelemetry:
    feedback_enabled = True

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.completed = False

    def start(self, **kwargs) -> QueryTelemetryContext:
        return QueryTelemetryContext(
            query_id=uuid.UUID("01922222-2222-7222-8222-222222222222"),
            session_id=uuid.UUID("01911111-1111-7111-8111-111111111111"),
        )

    def complete(self, context, **kwargs) -> None:
        self.started.set()
        assert self.release.wait(timeout=5)
        self.completed = True

    def fail(self, context, **kwargs) -> None:
        self.completed = True

    def feedback(self, **kwargs) -> FeedbackTelemetryReceipt:
        assert self.completed
        return FeedbackTelemetryReceipt(
            interaction_id=uuid.UUID("01933333-3333-7333-8333-333333333333"),
            result_rank=1,
        )


class _StartBlockingTelemetry(_BlockingTelemetry):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = Event()
        self.start_release = Event()

    def start(self, **kwargs) -> QueryTelemetryContext:
        self.start_entered.set()
        assert self.start_release.wait(timeout=2)
        return super().start(**kwargs)

    def complete(self, context, **kwargs) -> None:
        self.completed = True


class _TelemetryCoordinatedEncoder(_Encoder):
    def __init__(self, telemetry: _StartBlockingTelemetry) -> None:
        self.telemetry = telemetry

    def encode(self, image: Image.Image) -> QueryEncoding:
        assert self.telemetry.start_entered.wait(timeout=1)
        self.telemetry.start_release.set()
        return super().encode(image)


def test_service_encodes_both_spaces_and_returns_fixed_top_ten() -> None:
    search = _Search()
    service = _service(search)
    image = Image.new("RGB", (24, 24))
    try:
        response = asyncio.run(
            service.search_image(
                image,
                client_session_key="browser-session-1",
                upload_decode_ms=3.0,
            )
        )
    finally:
        service.close()

    assert search.top_k == 20
    assert search.artist_id is None
    assert response.result_count == 10
    assert response.results[0].match.subject_score == 0.7
    assert response.results[0].match.style_score == 0.6
    assert response.results[0].source_metadata.post_text == "Post about artwork 1"
    assert response.results[0].source_metadata.alt_text == "Alt text for artwork 1"
    assert response.model.index_alias == "artworks_active"
    assert response.timing.total_ms >= response.timing.upload_decode_ms
    assert response.timing.clip_encode_ms == 1.25
    assert response.timing.csd_encode_ms == 7.5
    assert response.timing.csd_preprocess_ms == 0.5
    assert response.timing.csd_tensor_transfer_ms == 0.1
    assert response.timing.csd_backbone_ms == 6.5
    assert response.timing.csd_projection_ms == 0.3
    assert response.timing.csd_output_ms == 0.1
    assert response.timing.embedding_normalize_ms == 0.25
    assert response.timing.telemetry_start_ms >= 0
    assert response.timing.telemetry_wait_ms >= 0
    with pytest.raises(ValueError, match="closed image"):
        image.getbbox()


def test_service_passes_artist_scope_to_vector_search() -> None:
    search = _Search()
    service = _service(search)
    try:
        response = asyncio.run(
            service.search_image(
                Image.new("RGB", (24, 24)),
                client_session_key="browser-session-1",
                upload_decode_ms=0.0,
                artist_id="artist-7",
            )
        )
    finally:
        service.close()

    assert search.artist_id == "artist-7"
    assert response.result_count == 10


def test_service_returns_before_telemetry_flush_and_feedback_waits_for_it() -> None:
    telemetry = _BlockingTelemetry()
    service = _service(_Search(), telemetry=telemetry)

    async def scenario():
        search = asyncio.create_task(
            service.search_image(
                Image.new("RGB", (24, 24)),
                client_session_key="browser-session-1",
                upload_decode_ms=0.0,
            )
        )
        response = await asyncio.wait_for(search, timeout=1)
        assert telemetry.started.wait(timeout=1)
        assert telemetry.completed is False
        feedback = asyncio.create_task(
            service.submit_feedback(
                client_session_key="browser-session-1",
                query_id=uuid.UUID(response.query_id),
                artwork_id="art-1",
                grade="great",
            )
        )
        await asyncio.sleep(0.01)
        assert feedback.done() is False
        telemetry.release.set()
        return await asyncio.wait_for(feedback, timeout=1)

    try:
        receipt = asyncio.run(scenario())
    finally:
        telemetry.release.set()
        service.close()

    assert receipt.result_rank == 1


def test_service_overlaps_telemetry_start_with_image_encoding() -> None:
    telemetry = _StartBlockingTelemetry()
    encoder = _TelemetryCoordinatedEncoder(telemetry)
    service = _service(_Search(), telemetry=telemetry, encoder=encoder)
    try:
        response = asyncio.run(
            service.search_image(
                Image.new("RGB", (24, 24)),
                client_session_key="browser-session-1",
                upload_decode_ms=0.0,
            )
        )
    finally:
        telemetry.start_release.set()
        service.close()

    assert response.result_count == 10
    assert telemetry.start_entered.is_set()
    assert response.timing.telemetry_start_ms >= 0
    assert response.timing.telemetry_wait_ms >= 0


def _service(search: _Search, *, telemetry=None, encoder=None) -> ImageSearchService:
    models = ModelConfig(
        clip_model_name="test/clip",
        clip_model_version="clip-v1",
        csd_model_name="test/csd",
        csd_model_version="csd-v1",
        csd_model_filename="model.safetensors",
        csd_model_sha256="a" * 64,
        csd_processor_name="test/processor",
        csd_processor_version="processor-v1",
    )
    qdrant = QdrantConfig(
        enabled=True,
        url=":memory:",
        clip_dimension=2,
        csd_dimension=2,
        datatype="float32",
        prefetch_limit=20,
        fusion_limit=20,
    )
    return ImageSearchService(
        api_config=SearchApiConfig(telemetry_enabled=False),
        model_config=models,
        qdrant_config=qdrant,
        encoder=encoder or _Encoder(),
        search=search,  # type: ignore[arg-type]
        catalog=_Catalog(),
        telemetry=telemetry or NoopSearchTelemetry(),
    )
