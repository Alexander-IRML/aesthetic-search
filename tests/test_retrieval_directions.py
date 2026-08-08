import numpy as np
import pytest

from artsearch.retrieval.directions import (
    apply_direction,
    mean_difference_direction,
    orthogonalize_directions,
)


def test_mean_difference_direction_averages_balanced_exemplars():
    target = np.array([[1.0, 1.0], [1.0, -1.0]], dtype=np.float32)
    neutral = np.array([[-1.0, 1.0], [-1.0, -1.0]], dtype=np.float32)

    direction = mean_difference_direction(target, neutral)

    assert direction == pytest.approx(np.array([1.0, 0.0], dtype=np.float32))


def test_apply_direction_moves_and_renormalizes_query():
    adjusted = apply_direction(
        np.array([0.0, 1.0], dtype=np.float32),
        np.array([1.0, 0.0], dtype=np.float32),
        1.0,
    )

    expected = np.array([1.0, 1.0], dtype=np.float32) / np.sqrt(2.0)
    assert adjusted == pytest.approx(expected)
    assert np.linalg.norm(adjusted) == pytest.approx(1.0)


def test_orthogonalize_directions_removes_axis_overlap():
    directions = np.array([[1.0, 0.0], [1.0, 1.0]], dtype=np.float32)

    orthogonal = orthogonalize_directions(directions)

    assert orthogonal[0] == pytest.approx(np.array([1.0, 0.0]))
    assert orthogonal[1] == pytest.approx(np.array([0.0, 1.0]))
    assert float(orthogonal[0] @ orthogonal[1]) == pytest.approx(0.0)


def test_orthogonalize_directions_rejects_dependent_axes():
    with pytest.raises(ValueError, match="linearly dependent"):
        orthogonalize_directions(
            np.array([[1.0, 0.0], [2.0, 0.0]], dtype=np.float32)
        )
