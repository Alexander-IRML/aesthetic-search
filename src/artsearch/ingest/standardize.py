from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

from artsearch.ingest.config import ImageConfig
from artsearch.ingest.transforms import TransformPlan, compute_transform


@dataclass(frozen=True)
class StandardizedImage:
    processed_path: Path
    orig_width: int
    orig_height: int
    transform: TransformPlan


def standardize_image(
    raw_path: str | Path,
    processed_path: str | Path,
    image_config: ImageConfig,
) -> StandardizedImage:
    raw = Path(raw_path)
    processed = Path(processed_path)
    processed.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(raw) as image:
        normalized = ImageOps.exif_transpose(image).convert("RGB")
        orig_width, orig_height = normalized.size
        plan = compute_transform(
            orig_width,
            orig_height,
            canonical_size=image_config.canonical_size,
            crop_threshold=image_config.crop_threshold,
        )

        cropped = normalized.crop(plan.crop_box)
        resized = cropped.resize(
            (plan.resized_width, plan.resized_height),
            resample=Image.Resampling.LANCZOS,
        )
        fill = _padding_fill(resized, image_config)
        canvas = Image.new("RGB", (plan.output_width, plan.output_height), fill)
        canvas.paste(resized, (plan.pad_left, plan.pad_top))
        canvas.save(
            processed,
            format="JPEG",
            quality=image_config.jpeg_quality,
            optimize=True,
        )

    return StandardizedImage(
        processed_path=processed,
        orig_width=orig_width,
        orig_height=orig_height,
        transform=plan,
    )


def _padding_fill(image: Image.Image, config: ImageConfig) -> tuple[int, int, int]:
    if config.padding_fill_strategy == "neutral_gray":
        value = config.neutral_gray_value
        return (value, value, value)
    if config.padding_fill_strategy == "average_edge_color":
        return _average_edge_color(image)
    raise ValueError(f"unknown padding fill strategy: {config.padding_fill_strategy}")


def _average_edge_color(image: Image.Image) -> tuple[int, int, int]:
    width, height = image.size
    strips = [
        image.crop((0, 0, width, 1)),
        image.crop((0, height - 1, width, height)),
        image.crop((0, 0, 1, height)),
        image.crop((width - 1, 0, width, height)),
    ]
    pixels = []
    for strip in strips:
        mean = ImageStat.Stat(strip).mean
        pixels.append(tuple(int(round(channel)) for channel in mean[:3]))
    return tuple(int(round(sum(pixel[i] for pixel in pixels) / len(pixels))) for i in range(3))