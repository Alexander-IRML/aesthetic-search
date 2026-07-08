from __future__ import annotations

import hashlib
from pathlib import Path

import imagehash
from PIL import Image, ImageOps


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def perceptual_hash(path: str | Path) -> str:
    with Image.open(path) as image:
        normalized = ImageOps.exif_transpose(image).convert("RGB")
        return str(imagehash.phash(normalized))


def phash_distance(left: str, right: str) -> int:
    return imagehash.hex_to_hash(left) - imagehash.hex_to_hash(right)