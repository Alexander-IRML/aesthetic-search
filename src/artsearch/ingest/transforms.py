from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransformPlan:
    original_width: int
    original_height: int
    crop_left: int
    crop_top: int
    crop_right: int
    crop_bottom: int
    cropped_width: int
    cropped_height: int
    scale_factor: float
    resized_width: int
    resized_height: int
    pad_left: int
    pad_top: int
    pad_right: int
    pad_bottom: int
    output_width: int
    output_height: int

    @property
    def crop_box(self) -> tuple[int, int, int, int]:
        """Pillow crop box for the retained region."""
        return (
            self.crop_left,
            self.crop_top,
            self.original_width - self.crop_right,
            self.original_height - self.crop_bottom,
        )


def compute_transform(
    width: int,
    height: int,
    *,
    canonical_size: int,
    crop_threshold: float,
) -> TransformPlan:
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if canonical_size <= 0:
        raise ValueError("canonical_size must be positive")
    if crop_threshold < 1:
        raise ValueError("crop_threshold must be >= 1")

    crop_left = crop_top = crop_right = crop_bottom = 0
    cropped_width = width
    cropped_height = height

    ratio = max(width, height) / min(width, height)
    if ratio > crop_threshold:
        if width > height:
            target_width = int(round(height * crop_threshold))
            excess = width - target_width
            crop_left = excess // 2
            crop_right = excess - crop_left
            cropped_width = width - crop_left - crop_right
        else:
            target_height = int(round(width * crop_threshold))
            excess = height - target_height
            crop_top = excess // 2
            crop_bottom = excess - crop_top
            cropped_height = height - crop_top - crop_bottom

    long_side = max(cropped_width, cropped_height)
    scale_factor = canonical_size / long_side
    resized_width = max(1, int(round(cropped_width * scale_factor)))
    resized_height = max(1, int(round(cropped_height * scale_factor)))

    pad_x = canonical_size - resized_width
    pad_y = canonical_size - resized_height
    if pad_x < 0 or pad_y < 0:
        raise ValueError("computed resized dimensions exceed canonical size")

    pad_left = pad_x // 2
    pad_right = pad_x - pad_left
    pad_top = pad_y // 2
    pad_bottom = pad_y - pad_top

    return TransformPlan(
        original_width=width,
        original_height=height,
        crop_left=crop_left,
        crop_top=crop_top,
        crop_right=crop_right,
        crop_bottom=crop_bottom,
        cropped_width=cropped_width,
        cropped_height=cropped_height,
        scale_factor=scale_factor,
        resized_width=resized_width,
        resized_height=resized_height,
        pad_left=pad_left,
        pad_top=pad_top,
        pad_right=pad_right,
        pad_bottom=pad_bottom,
        output_width=canonical_size,
        output_height=canonical_size,
    )


def original_to_standardized(
    x: float,
    y: float,
    plan: TransformPlan,
) -> tuple[float, float]:
    return (
        (x - plan.crop_left) * plan.scale_factor + plan.pad_left,
        (y - plan.crop_top) * plan.scale_factor + plan.pad_top,
    )


def standardized_to_original(
    x: float,
    y: float,
    plan: TransformPlan,
) -> tuple[float, float]:
    return (
        (x - plan.pad_left) / plan.scale_factor + plan.crop_left,
        (y - plan.pad_top) / plan.scale_factor + plan.crop_top,
    )