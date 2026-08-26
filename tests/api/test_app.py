from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
import uuid

import httpx
from PIL import Image

from artsearch.api.app import create_app
from artsearch.api.catalog import CatalogArtwork
from artsearch.api.config import SearchApiConfig
from artsearch.api.schemas import (
    ArtworkSearchResult,
    ImageSearchResponse,
    MatchEvidence,
    ModelContract,
    SearchTiming,
)
from artsearch.api.telemetry import FeedbackTelemetryReceipt
from artsearch.production.config import QdrantConfig


class _Catalog:
    def __init__(self, image_path: Path | None = None) -> None:
        self.image_path = image_path

    def get_many(self, artwork_ids):
        return {
            value: CatalogArtwork(value, "artist", "Artist", f"/api/artworks/{value}/image")
            for value in artwork_ids
        }

    def local_image_path(self, artwork_id):
        return self.image_path if artwork_id == "art-1" else None


class _Service:
    def __init__(self, image_path: Path | None = None) -> None:
        self.ready = True
        self.model_bundle = "b" * 64
        self.qdrant_config = QdrantConfig(enabled=True, alias_name="artworks_active")
        self.catalog = _Catalog(image_path)

    async def search_image(self, image, *, client_session_key, upload_decode_ms):
        assert image.mode == "RGB"
        assert client_session_key == "test-session-1234"
        return ImageSearchResponse(
            api_version="1.0.0",
            query_id="query-1234",
            session_id="session-1234",
            result_count=1,
            results=[
                ArtworkSearchResult(
                    artwork_id="art-1",
                    artist_id="artist-1",
                    artist_name="Artist One",
                    rank=1,
                    image_url="/api/artworks/art-1/image",
                    content_class="finished_illustration",
                    match=MatchEvidence(subject_score=0.8, style_score=0.7),
                )
            ],
            model=ModelContract(
                bundle_version="b" * 64,
                index_alias="artworks_active",
                clip_model="clip",
                clip_revision="clip-v1",
                csd_model="csd",
                csd_revision="csd-v1",
            ),
            timing=SearchTiming(
                upload_decode_ms=upload_decode_ms,
                queue_ms=0,
                encode_ms=2,
                vector_search_ms=1,
                catalog_ms=1,
                total_ms=5,
            ),
        )

    async def submit_feedback(
        self,
        *,
        client_session_key,
        query_id,
        artwork_id,
        grade,
    ):
        assert client_session_key == "test-session-1234"
        assert artwork_id == "art-1"
        assert grade in {"great", "good", "bad"}
        return FeedbackTelemetryReceipt(
            interaction_id=uuid.UUID("01933333-3333-7333-8333-333333333333"),
            result_rank=1,
        )


class _BrokenService(_Service):
    async def search_image(self, image, *, client_session_key, upload_decode_ms):
        raise ValueError("private implementation detail")


def test_upload_frontend_and_search_contract(tmp_path: Path) -> None:
    image_path = tmp_path / "result.jpg"
    Image.new("RGB", (12, 12), "white").save(image_path)
    service = _Service(image_path)
    app = create_app(api_config=SearchApiConfig(telemetry_enabled=False), service=service)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            frontend = await client.get("/search")
            response = await client.post(
                "/api/search-by-image",
                files={"image": ("query.png", _image_bytes(), "image/png")},
                data={"session_key": "test-session-1234"},
            )
            image_response = await client.get("/api/artworks/art-1/image")
        return frontend, response, image_response

    frontend, response, image_response = asyncio.run(scenario())

    assert frontend.status_code == 200
    assert "Drop an artwork here" in frontend.text
    assert 'data-grade="great"' in frontend.text
    assert response.status_code == 200
    assert response.json()["results"][0]["artist_name"] == "Artist One"
    assert response.json()["result_count"] == 1
    assert response.headers["x-content-type-options"] == "nosniff"
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/jpeg"


def test_result_feedback_records_a_graded_relevance_event() -> None:
    app = create_app(
        api_config=SearchApiConfig(telemetry_enabled=True),
        service=_Service(),
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/result-feedback",
                json={
                    "query_id": "01922222-2222-7222-8222-222222222222",
                    "artwork_id": "art-1",
                    "session_key": "test-session-1234",
                    "grade": "great",
                },
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json() == {
        "status": "recorded",
        "interaction_id": "01933333-3333-7333-8333-333333333333",
        "query_id": "01922222-2222-7222-8222-222222222222",
        "artwork_id": "art-1",
        "result_rank": 1,
        "grade": "great",
        "relevance": 2,
    }


def test_api_rejects_bad_media_and_rate_limits() -> None:
    config = SearchApiConfig(telemetry_enabled=False, rate_limit_requests=2)
    app = create_app(api_config=config, service=_Service())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            bad = await client.post(
                "/api/search-by-image",
                files={"image": ("query.txt", b"bad", "text/plain")},
                data={"session_key": "test-session-1234"},
            )
            first = await client.post(
                "/api/search-by-image",
                files={"image": ("query.png", _image_bytes(), "image/png")},
                data={"session_key": "test-session-1234"},
            )
            limited = await client.post(
                "/api/search-by-image",
                files={"image": ("query.png", _image_bytes(), "image/png")},
                data={"session_key": "test-session-1234"},
            )
        return bad, first, limited

    bad, first, limited = asyncio.run(scenario())

    assert bad.status_code == 415
    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"


def test_api_returns_stable_sanitized_error_for_unexpected_failure() -> None:
    app = create_app(
        api_config=SearchApiConfig(telemetry_enabled=False),
        service=_BrokenService(),
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/search-by-image",
                files={"image": ("query.png", _image_bytes(), "image/png")},
                data={"session_key": "test-session-1234"},
            )

    response = asyncio.run(scenario())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private implementation detail" not in response.text


def test_frontend_preview_serves_ui_without_search_dependencies() -> None:
    app = create_app(
        api_config=SearchApiConfig(telemetry_enabled=False),
        initialize_search_service=False,
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            frontend = await client.get("/search")
            readiness = await client.get("/health/ready")
            search = await client.post(
                "/api/search-by-image",
                files={"image": ("query.png", _image_bytes(), "image/png")},
                data={"session_key": "test-session-1234"},
            )
        return frontend, readiness, search

    frontend, readiness, search = asyncio.run(scenario())

    assert frontend.status_code == 200
    assert readiness.status_code == 503
    assert search.status_code == 503
    assert search.json()["error"]["code"] == "service_starting"


def _image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 16), "white").save(output, format="PNG")
    return output.getvalue()
