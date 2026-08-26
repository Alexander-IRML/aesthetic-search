from __future__ import annotations

import asyncio
from io import BytesIO

from PIL import Image
import pytest

from artsearch.api.config import SearchApiConfig
from artsearch.api.errors import InvalidUploadError, UnsupportedUploadError, UploadTooLargeError
from artsearch.api.image_input import read_validated_image


class _Upload:
    def __init__(self, data: bytes, content_type: str) -> None:
        self.data = data
        self.content_type = content_type
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.data)
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def test_upload_decodes_to_rgb_without_persisting() -> None:
    upload = _Upload(_image_bytes("PNG", size=(20, 12), mode="RGBA"), "image/png")

    result = asyncio.run(read_validated_image(upload, SearchApiConfig()))

    assert result.image.mode == "RGB"
    assert (result.width, result.height) == (20, 12)
    assert result.mime_type == "image/png"


def test_upload_rejects_declared_and_decoded_mime_mismatch() -> None:
    upload = _Upload(_image_bytes("PNG"), "image/jpeg")

    with pytest.raises(UnsupportedUploadError, match="does not match"):
        asyncio.run(read_validated_image(upload, SearchApiConfig()))


def test_upload_enforces_byte_and_pixel_limits() -> None:
    data = _image_bytes("PNG", size=(20, 20))
    with pytest.raises(UploadTooLargeError, match="upload limit"):
        asyncio.run(
            read_validated_image(
                _Upload(data, "image/png"),
                SearchApiConfig(max_upload_bytes=10),
            )
        )

    with pytest.raises(UploadTooLargeError, match="pixel limit"):
        asyncio.run(
            read_validated_image(
                _Upload(data, "image/png"),
                SearchApiConfig(max_image_pixels=100),
            )
        )


def test_upload_rejects_corrupt_image() -> None:
    with pytest.raises(InvalidUploadError, match="valid image"):
        asyncio.run(
            read_validated_image(
                _Upload(b"not an image", "image/png"),
                SearchApiConfig(),
            )
        )


def _image_bytes(format_name: str, *, size=(8, 8), mode: str = "RGB") -> bytes:
    image = Image.new(mode, size, (30, 90, 140, 255) if mode == "RGBA" else (30, 90, 140))
    output = BytesIO()
    image.save(output, format=format_name)
    return output.getvalue()
