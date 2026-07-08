from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


@dataclass(frozen=True)
class ImageAudit:
    path: Path
    ok: bool
    width: int | None = None
    height: int | None = None
    error: str | None = None


def audit_image_file(path: str | Path, *, min_dimension: int = 8) -> ImageAudit:
    image_path = Path(path)
    try:
        with Image.open(image_path) as image:
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            width, height = normalized.size
            if width < min_dimension or height < min_dimension:
                return ImageAudit(
                    path=image_path,
                    ok=False,
                    width=width,
                    height=height,
                    error="image dimensions are below minimum threshold",
                )
            return ImageAudit(path=image_path, ok=True, width=width, height=height)
    except Exception as exc:
        return ImageAudit(path=image_path, ok=False, error=str(exc))


def supported_image(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}