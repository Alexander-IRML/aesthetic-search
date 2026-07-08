import pytest

from artsearch.ingest.transforms import (
    compute_transform,
    original_to_standardized,
    standardized_to_original,
)


def test_wide_image_is_scaled_and_vertically_padded_without_crop():
    plan = compute_transform(800, 400, canonical_size=448, crop_threshold=2.5)

    assert plan.crop_left == 0
    assert plan.crop_right == 0
    assert plan.scale_factor == 0.56
    assert (plan.resized_width, plan.resized_height) == (448, 224)
    assert (plan.pad_left, plan.pad_top, plan.pad_right, plan.pad_bottom) == (0, 112, 0, 112)


def test_tall_image_is_scaled_and_horizontally_padded_without_crop():
    plan = compute_transform(400, 800, canonical_size=448, crop_threshold=2.5)

    assert plan.scale_factor == 0.56
    assert (plan.resized_width, plan.resized_height) == (224, 448)
    assert (plan.pad_left, plan.pad_top, plan.pad_right, plan.pad_bottom) == (112, 0, 112, 0)


def test_extreme_portrait_is_center_cropped_before_scaling():
    plan = compute_transform(400, 1200, canonical_size=448, crop_threshold=2.5)

    assert (plan.crop_left, plan.crop_top, plan.crop_right, plan.crop_bottom) == (0, 100, 0, 100)
    assert (plan.cropped_width, plan.cropped_height) == (400, 1000)
    assert plan.scale_factor == 0.448
    assert (plan.resized_width, plan.resized_height) == (179, 448)
    assert (plan.pad_left, plan.pad_top, plan.pad_right, plan.pad_bottom) == (134, 0, 135, 0)


def test_coordinate_mapping_round_trips_through_transform():
    plan = compute_transform(400, 1200, canonical_size=448, crop_threshold=2.5)
    original = (200.0, 500.0)

    standardized = original_to_standardized(*original, plan)
    round_trip = standardized_to_original(*standardized, plan)

    assert round_trip == pytest.approx(original)