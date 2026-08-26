from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Protocol
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from artsearch.api.config import SearchApiConfig
from artsearch.api.errors import (
    InvalidUploadError,
    UnsupportedUploadError,
    UploadTooLargeError,
)


FORMAT_MIME_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class AsyncUpload(Protocol):
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True)
class ValidatedImage:
    image: Image.Image
    width: int
    height: int
    byte_size: int
    mime_type: str


async def read_validated_image(
    upload: AsyncUpload,
    config: SearchApiConfig,
    *,
    chunk_size: int = 1024 * 1024,
) -> ValidatedImage:
    declared_mime = (upload.content_type or "").split(";", 1)[0].strip().lower()
    if declared_mime not in config.allowed_mime_types:
        raise UnsupportedUploadError("Upload a JPEG, PNG, or WebP image using its image MIME type.")

    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(chunk_size):
        total += len(chunk)
        if total > config.max_upload_bytes:
            raise UploadTooLargeError(
                f"Image exceeds the {config.max_upload_bytes // 1_000_000} MB upload limit."
            )
        chunks.append(chunk)
    if total == 0:
        raise InvalidUploadError("The uploaded image is empty.")

    data = b"".join(chunks)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                actual_mime = FORMAT_MIME_TYPES.get(probe.format or "")
                if actual_mime not in config.allowed_mime_types:
                    raise UnsupportedUploadError("The decoded image format is not supported.")
                if actual_mime != declared_mime:
                    raise UnsupportedUploadError(
                        "The declared MIME type does not match the decoded image."
                    )
                width, height = probe.size
                if width <= 0 or height <= 0:
                    raise InvalidUploadError("The uploaded image has invalid dimensions.")
                if width * height > config.max_image_pixels:
                    raise UploadTooLargeError(
                        f"Image exceeds the {config.max_image_pixels:,}-pixel limit."
                    )
                if int(getattr(probe, "n_frames", 1)) != 1:
                    raise UnsupportedUploadError("Animated images are not supported.")
                probe.verify()

            with Image.open(BytesIO(data)) as decoded:
                decoded.load()
                rgb = ImageOps.exif_transpose(decoded).convert("RGB").copy()
    except (InvalidUploadError, UnsupportedUploadError, UploadTooLargeError):
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise UploadTooLargeError("The image exceeds safe decoding limits.") from exc
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise InvalidUploadError("The uploaded file is not a valid image.") from exc

    return ValidatedImage(
        image=rgb,
        width=rgb.width,
        height=rgb.height,
        byte_size=total,
        mime_type=actual_mime,
    )
