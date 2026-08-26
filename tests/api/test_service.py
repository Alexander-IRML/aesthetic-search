from __future__ import annotations

import asyncio

import numpy as np
from PIL import Image
import pytest

from artsearch.api.catalog import CatalogArtwork
from artsearch.api.config import SearchApiConfig
from artsearch.api.service import ImageSearchService
from artsearch.api.telemetry import NoopSearchTelemetry
from artsearch.embed.storage import ImageEmbeddings
from artsearch.ingest.config import ModelConfig
from artsearch.production.config import QdrantConfig
from artsearch.retrieval.qdrant import QdrantSearchHit


class _Encoder:
    def encode(self, image: Image.Image) -> ImageEmbeddings:
        assert image.mode == "RGB"
        return ImageEmbeddings(
            clip_vector=np.array([1.0, 0.0], dtype=np.float32),
            csd_style=np.array([0.0, 1.0], dtype=np.float32),
        )


class _Search:
    def __init__(self) -> None:
        self.top_k = 0

    def search_by_vectors(self, clip, style, *, top_k: int):
        assert np.asarray(clip).tolist() == [1.0, 0.0]
        assert np.asarray(style).tolist() == [0.0, 1.0]
        self.top_k = top_k
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
            )
            for index, artwork_id in enumerate(artwork_ids, start=1)
        }

    def local_image_path(self, artwork_id):
        return None


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
    assert response.result_count == 10
    assert response.results[0].match.subject_score == 0.7
    assert response.results[0].match.style_score == 0.6
    assert response.model.index_alias == "artworks_active"
    assert response.timing.total_ms >= response.timing.upload_decode_ms
    with pytest.raises(ValueError, match="closed image"):
        image.getbbox()


def _service(search: _Search) -> ImageSearchService:
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
        encoder=_Encoder(),
        search=search,  # type: ignore[arg-type]
        catalog=_Catalog(),
        telemetry=NoopSearchTelemetry(),
    )
