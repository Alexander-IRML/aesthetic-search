from __future__ import annotations

from io import BytesIO
import ipaddress
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
import warnings

import asyncio

import httpx
import imagehash
from PIL import Image, ImageOps, UnidentifiedImageError

from artsearch.artwork_filter.config import ArtworkFilterConfig
from artsearch.artwork_filter.errors import (
    DownloadError,
    DownloadTooLargeError,
    ImageDecodeError,
    ImageValidationError,
    UnsupportedMediaError,
)
from artsearch.artwork_filter.hashing import sha256_bytes
from artsearch.artwork_filter.schemas import ImageCandidate, LoadedImage


class ImageLoader(Protocol):
    async def load(self, candidate: ImageCandidate) -> LoadedImage: ...


class HttpOrLocalImageLoader:
    def __init__(
        self,
        config: ArtworkFilterConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ValueError("provide either http_client or transport, not both")
        self.config = config
        self._owns_client = http_client is None
        self._client = http_client
        self._transport = transport

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def load(self, candidate: ImageCandidate) -> LoadedImage:
        if candidate.local_path is not None:
            try:
                local_path = Path(candidate.local_path)
                if local_path.stat().st_size > self.config.downloads.max_bytes:
                    raise DownloadTooLargeError("local image exceeded configured byte cap")
                data = local_path.read_bytes()
            except DownloadTooLargeError:
                raise
            except OSError as exc:
                raise DownloadError(f"could not read local image: {exc}") from exc
            return self._load_bytes(
                candidate,
                data,
                source_url=None,
                content_type=candidate.mime_type,
            )

        last_error: DownloadError | UnsupportedMediaError | ImageDecodeError | None = None
        for url in self._candidate_urls(candidate):
            try:
                data, content_type = await self._download(url, source=candidate.source)
                return self._load_bytes(
                    candidate,
                    data,
                    source_url=url,
                    content_type=content_type,
                )
            except (DownloadError, UnsupportedMediaError, ImageDecodeError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise DownloadError("candidate has no usable URL")

    def _candidate_urls(self, candidate: ImageCandidate) -> list[str]:
        if self.config.downloads.prefer_thumbnail:
            ordered = [candidate.thumbnail_url, candidate.fullsize_url]
        else:
            ordered = [candidate.fullsize_url, candidate.thumbnail_url]
        available = list(dict.fromkeys(url for url in ordered if url))
        if not self.config.downloads.allow_fullsize_fallback:
            return available[:1]
        return available

    async def _download(self, url: str, *, source: str) -> tuple[bytes, str | None]:
        for attempt in range(self.config.downloads.max_retries + 1):
            try:
                return await self._download_once(url, source=source)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if attempt < self.config.downloads.max_retries and _retryable_status(status):
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                raise DownloadError(f"HTTP {status} while downloading image") from exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < self.config.downloads.max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                raise DownloadError(str(exc)) from exc
        raise DownloadError("download retries exhausted")

    async def _download_once(self, url: str, *, source: str) -> tuple[bytes, str | None]:
        current_url = url
        redirect_statuses = {301, 302, 303, 307, 308}
        for redirect_count in range(self.config.downloads.max_redirects + 1):
            _validate_remote_url(current_url, source=source, config=self.config)
            client = self._http_client()
            async with client.stream("GET", current_url) as response:
                if response.status_code in redirect_statuses:
                    location = response.headers.get("location")
                    if not location:
                        raise DownloadError("image redirect did not include a location")
                    if redirect_count >= self.config.downloads.max_redirects:
                        raise DownloadError("image download exceeded redirect limit")
                    current_url = str(response.url.join(location))
                    continue

                response.raise_for_status()
                content_type = response.headers.get("content-type")
                _validate_content_type(content_type)
                declared_size = _content_length(response.headers.get("content-length"))
                if declared_size is not None and declared_size > self.config.downloads.max_bytes:
                    raise DownloadTooLargeError("download exceeded configured byte cap")

                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.config.downloads.max_bytes:
                        raise DownloadTooLargeError("download exceeded configured byte cap")
                    chunks.append(chunk)
                return b"".join(chunks), content_type
        raise DownloadError("image download exceeded redirect limit")

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.downloads.timeout_seconds),
                headers={"User-Agent": self.config.downloads.user_agent},
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=self.config.downloads.max_concurrency,
                    max_keepalive_connections=self.config.downloads.max_concurrency,
                ),
                transport=self._transport,
            )
        return self._client

    def _load_bytes(
        self,
        candidate: ImageCandidate,
        data: bytes,
        *,
        source_url: str | None,
        content_type: str | None,
    ) -> LoadedImage:
        if len(data) > self.config.downloads.max_bytes:
            raise DownloadTooLargeError("image exceeded configured byte cap")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    width, height = image.size
                    if width * height > self.config.media.max_pixels:
                        raise ImageValidationError("image exceeded configured pixel cap")
                    is_animated = bool(getattr(image, "n_frames", 1) > 1)
                    image.seek(0)
                    image.load()
                    original_format = image.format
                    mime_type = Image.MIME.get(original_format or "", content_type)
                    _validate_content_type(mime_type)
                    normalized = ImageOps.exif_transpose(image)
                    rgb = _to_rgb(
                        normalized,
                        self.config.media.convert_rgba_to_rgb_background,
                    )
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ImageDecodeError(str(exc)) from exc
        except Image.DecompressionBombWarning as exc:
            raise ImageValidationError(str(exc)) from exc

        return LoadedImage(
            candidate_id=candidate.candidate_id,
            rgb_image=rgb,
            width=rgb.width,
            height=rgb.height,
            format=original_format,
            mime_type=mime_type,
            byte_size=len(data),
            sha256=sha256_bytes(data),
            perceptual_hash=str(imagehash.phash(rgb)),
            source_url=source_url,
            is_animated=is_animated,
        )


def _to_rgb(image: Image.Image, background: str) -> Image.Image:
    if image.mode == "RGB":
        return image.copy()
    if image.mode in {"RGBA", "LA"}:
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, background)
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")
    return image.convert("RGB")


def _validate_content_type(content_type: str | None) -> None:
    if not content_type:
        return
    normalized = content_type.split(";", maxsplit=1)[0].strip().lower()
    if not normalized.startswith("image/") or normalized == "image/svg+xml":
        raise UnsupportedMediaError(f"unsupported content type: {content_type}")


def _content_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _retryable_status(status: int) -> bool:
    return status in {408, 425, 429} or status >= 500


def _validate_remote_url(url: str, *, source: str, config: ArtworkFilterConfig) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DownloadError("remote images require an absolute HTTPS URL")
    hostname = parsed.hostname.casefold().rstrip(".")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise DownloadError("remote image URL must not target a private or local address")
    if source == "bluesky" and not any(
        hostname == allowed.casefold().rstrip(".")
        or hostname.endswith("." + allowed.casefold().rstrip("."))
        for allowed in config.downloads.allowed_bluesky_image_hosts
    ):
        raise DownloadError("Bluesky image URL host is not in the configured allowlist")
