from __future__ import annotations

import asyncio
from io import BytesIO
import os
from pathlib import Path
import re
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict

from artsearch.artwork_filter.config import ArtworkFilterConfig
from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.hashing import sha256_bytes
from artsearch.artwork_filter.image_io import HttpOrLocalImageLoader, ImageLoader
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate, LoadedImage


class RoutedImage(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    candidate_id: str
    source_cid: str | None = None
    target: Literal["corpus", "review"]
    status: Literal["stored", "duplicate", "error"]
    local_path: Path | None = None
    image_sha256: str | None = None
    perceptual_hash: str | None = None
    width: int | None = None
    height: int | None = None
    error_type: str | None = None
    error_message: str | None = None


class CorpusRouter:
    """Materialize full-size ACCEPT/REVIEW images while leaving rejects as evidence only."""

    def __init__(
        self,
        config: ArtworkFilterConfig,
        *,
        raw_dir: str | Path,
        image_loader: ImageLoader | None = None,
        jpeg_quality: int = 95,
        download_review_images: bool | None = None,
    ) -> None:
        self.config = config
        self.raw_dir = Path(raw_dir)
        self.review_dir = config.storage.review_image_dir
        self.jpeg_quality = jpeg_quality
        self.download_review_images = (
            config.storage.download_review_images
            if download_review_images is None
            else download_review_images
        )
        self._owns_image_loader = image_loader is None
        if image_loader is None:
            fullsize_config = config.model_copy(deep=True)
            fullsize_config.downloads.prefer_thumbnail = False
            image_loader = HttpOrLocalImageLoader(fullsize_config)
        self.image_loader = image_loader

    async def __aenter__(self) -> "CorpusRouter":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._owns_image_loader:
            return
        close = getattr(self.image_loader, "aclose", None)
        if close is not None:
            await close()

    async def route_many(
        self,
        candidates: list[ImageCandidate],
        results: list[FilterResult],
    ) -> list[RoutedImage]:
        if len(candidates) != len(results):
            raise ValueError("candidates and results must have the same length")
        semaphore = asyncio.Semaphore(self.config.downloads.max_concurrency)

        async def route_one(
            candidate: ImageCandidate,
            result: FilterResult,
        ) -> RoutedImage | None:
            if result.decision == FilterDecision.ACCEPT:
                target = "corpus"
            elif result.decision == FilterDecision.REVIEW and self.download_review_images:
                target = "review"
            else:
                return None
            async with semaphore:
                return await self._route(candidate, result, target)

        routed = await asyncio.gather(
            *(
                route_one(candidate, result)
                for candidate, result in zip(candidates, results, strict=True)
            )
        )
        return [item for item in routed if item is not None]

    async def _route(
        self,
        candidate: ImageCandidate,
        result: FilterResult,
        target: str,
    ) -> RoutedImage:
        destination = self._destination(candidate, result, target)
        loaded: LoadedImage | None = None
        try:
            if destination.exists():
                loaded = await self.image_loader.load(
                    ImageCandidate(
                        candidate_id=candidate.candidate_id,
                        local_path=destination,
                        source="local",
                    )
                )
                return _routed_image(candidate, target, destination, loaded)

            source_candidate = candidate
            if candidate.local_path is None and candidate.fullsize_url:
                source_candidate = candidate.model_copy(update={"thumbnail_url": None})
            loaded = await self.image_loader.load(source_candidate)
            payload = _jpeg_bytes(loaded, quality=self.jpeg_quality)
            _atomic_write(destination, payload)
            return RoutedImage(
                candidate_id=candidate.candidate_id,
                source_cid=candidate.post_cid,
                target=target,
                status="stored",
                local_path=destination,
                image_sha256=sha256_bytes(payload),
                perceptual_hash=loaded.perceptual_hash,
                width=loaded.width,
                height=loaded.height,
            )
        except Exception as exc:
            return RoutedImage(
                candidate_id=candidate.candidate_id,
                source_cid=candidate.post_cid,
                target=target,
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        finally:
            if loaded is not None:
                loaded.rgb_image.close()

    def _destination(
        self,
        candidate: ImageCandidate,
        result: FilterResult,
        target: str,
    ) -> Path:
        filename = routed_image_filename(candidate)
        if target == "corpus":
            return self.raw_dir / bluesky_artist_folder(candidate) / filename
        return self.review_dir / _safe_component(result.route) / filename


def bluesky_artist_id(candidate: ImageCandidate) -> str:
    identity = candidate.author_did or candidate.author_handle or candidate.candidate_id
    return f"bsky_{sha256_bytes(identity.encode('utf-8'))[:24]}"


def bluesky_artist_folder(candidate: ImageCandidate) -> str:
    return bluesky_artist_id(candidate)


def safe_candidate_filename(candidate_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", candidate_id):
        return candidate_id
    return sha256_bytes(candidate_id.encode("utf-8"))


def routed_image_filename(candidate: ImageCandidate) -> str:
    candidate_name = safe_candidate_filename(candidate.candidate_id)
    if not candidate.post_cid:
        return f"{candidate_name}.jpg"
    cid_suffix = sha256_bytes(candidate.post_cid.encode("utf-8"))[:12]
    return f"{candidate_name}-{cid_suffix}.jpg"


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return normalized or "review"


def _jpeg_bytes(loaded: LoadedImage, *, quality: int) -> bytes:
    output = BytesIO()
    loaded.rgb_image.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _routed_image(
    candidate: ImageCandidate,
    target: str,
    destination: Path,
    loaded: LoadedImage,
) -> RoutedImage:
    return RoutedImage(
        candidate_id=candidate.candidate_id,
        source_cid=candidate.post_cid,
        target=target,
        status="stored",
        local_path=destination,
        image_sha256=loaded.sha256,
        perceptual_hash=loaded.perceptual_hash,
        width=loaded.width,
        height=loaded.height,
    )
