import asyncio
from io import BytesIO

import httpx
import pytest
from PIL import Image

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.errors import (
    DownloadError,
    DownloadTooLargeError,
    ImageDecodeError,
    ImageValidationError,
    UnsupportedMediaError,
)
from artsearch.artwork_filter.image_io import HttpOrLocalImageLoader
from artsearch.artwork_filter.schemas import ImageCandidate


def test_loads_local_image_and_computes_hash(tmp_path):
    image_path = tmp_path / "image.png"
    Image.new("RGBA", (320, 320), (255, 0, 0, 128)).save(image_path)
    config = load_artwork_filter_config()
    loader = HttpOrLocalImageLoader(config)

    loaded = asyncio.run(
        loader.load(ImageCandidate(candidate_id="candidate", local_path=image_path, source="local"))
    )

    assert loaded.width == 320
    assert loaded.height == 320
    assert loaded.format == "PNG"
    assert loaded.sha256
    assert loaded.perceptual_hash
    assert loaded.rgb_image.mode == "RGB"


def test_rejects_invalid_local_image_bytes(tmp_path):
    image_path = tmp_path / "bad.jpg"
    image_path.write_bytes(b"not an image")
    config = load_artwork_filter_config()
    loader = HttpOrLocalImageLoader(config)

    with pytest.raises(ImageDecodeError):
        asyncio.run(loader.load(ImageCandidate(candidate_id="candidate", local_path=image_path)))


def test_http_loader_falls_back_from_missing_thumbnail_to_fullsize():
    payload = _image_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("thumb.jpg"):
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "image/jpeg"},
            request=request,
        )

    config = load_artwork_filter_config()
    config.downloads.max_retries = 0
    loader = HttpOrLocalImageLoader(config, transport=httpx.MockTransport(handler))
    candidate = ImageCandidate(
        candidate_id="candidate",
        thumbnail_url="https://cdn.example/thumb.jpg",
        fullsize_url="https://cdn.example/full.jpg",
        source="test",
    )

    loaded = asyncio.run(loader.load(candidate))

    assert loaded.source_url == candidate.fullsize_url
    assert loaded.width == 320


def test_http_loader_enforces_declared_byte_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x",
            headers={"content-type": "image/jpeg", "content-length": "100"},
            request=request,
        )

    config = load_artwork_filter_config()
    config.downloads.max_bytes = 10
    loader = HttpOrLocalImageLoader(config, transport=httpx.MockTransport(handler))

    with pytest.raises(DownloadTooLargeError):
        asyncio.run(
            loader.load(
                ImageCandidate(
                    candidate_id="candidate",
                    fullsize_url="https://cdn.example/x",
                    source="test",
                )
            )
        )


def test_http_loader_returns_typed_timeout_and_rejects_svg():
    config = load_artwork_filter_config()
    config.downloads.max_retries = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    timeout_loader = HttpOrLocalImageLoader(config, transport=httpx.MockTransport(timeout))
    with pytest.raises(DownloadError):
        asyncio.run(
            timeout_loader.load(
                ImageCandidate(
                    candidate_id="candidate",
                    fullsize_url="https://cdn.example/x",
                    source="test",
                )
            )
        )

    def svg(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<svg/>",
            headers={"content-type": "image/svg+xml"},
            request=request,
        )

    svg_loader = HttpOrLocalImageLoader(config, transport=httpx.MockTransport(svg))
    with pytest.raises(UnsupportedMediaError):
        asyncio.run(
            svg_loader.load(
                ImageCandidate(
                    candidate_id="candidate",
                    fullsize_url="https://cdn.example/x",
                    source="test",
                )
            )
        )


def test_local_loader_enforces_pixel_cap(tmp_path):
    image_path = tmp_path / "large.png"
    Image.new("RGB", (20, 20), "white").save(image_path)
    config = load_artwork_filter_config()
    config.media.max_pixels = 100
    loader = HttpOrLocalImageLoader(config)

    with pytest.raises(ImageValidationError):
        asyncio.run(loader.load(ImageCandidate(candidate_id="candidate", local_path=image_path)))


def test_local_loader_enforces_byte_cap_before_decode(tmp_path):
    image_path = tmp_path / "large.jpg"
    image_path.write_bytes(b"x" * 11)
    config = load_artwork_filter_config()
    config.downloads.max_bytes = 10
    loader = HttpOrLocalImageLoader(config)

    with pytest.raises(DownloadTooLargeError):
        asyncio.run(loader.load(ImageCandidate(candidate_id="candidate", local_path=image_path)))


def test_bluesky_loader_rejects_unapproved_host_and_redirect_target():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://untrusted.example/image.jpg"},
            request=request,
        )

    config = load_artwork_filter_config()
    loader = HttpOrLocalImageLoader(config, transport=httpx.MockTransport(handler))

    with pytest.raises(DownloadError, match="allowlist"):
        asyncio.run(
            loader.load(
                ImageCandidate(
                    candidate_id="candidate",
                    fullsize_url="https://untrusted.example/image.jpg",
                    source="bluesky",
                )
            )
        )
    assert requests == []

    with pytest.raises(DownloadError, match="allowlist"):
        asyncio.run(
            loader.load(
                ImageCandidate(
                    candidate_id="candidate",
                    fullsize_url="https://cdn.bsky.app/image.jpg",
                    source="bluesky",
                )
            )
        )
    assert requests == ["https://cdn.bsky.app/image.jpg"]


def _image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (320, 320), "white").save(output, format="JPEG")
    return output.getvalue()
