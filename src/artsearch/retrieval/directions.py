from __future__ import annotations

import numpy as np


def mean_difference_direction(
    target_embeddings: np.ndarray,
    neutral_embeddings: np.ndarray,
) -> np.ndarray:
    """Build one normalized axis from balanced target and neutral exemplars."""
    target = _normalized_matrix(target_embeddings, "target_embeddings")
    neutral = _normalized_matrix(neutral_embeddings, "neutral_embeddings")
    if target.shape[1] != neutral.shape[1]:
        raise ValueError("target and neutral embeddings must have the same dimension")
    return _normalized_vector(target.mean(axis=0) - neutral.mean(axis=0), "direction")


def orthogonalize_directions(directions: np.ndarray) -> np.ndarray:
    """Remove overlap between ordered exploration axes with Gram-Schmidt."""
    matrix = _normalized_matrix(directions, "directions")
    orthogonal: list[np.ndarray] = []
    for index, direction in enumerate(matrix):
        residual = direction.copy()
        for basis in orthogonal:
            residual -= float(residual @ basis) * basis
        norm = float(np.linalg.norm(residual))
        if norm <= 1e-8:
            raise ValueError(
                f"direction {index} is linearly dependent on earlier directions"
            )
        orthogonal.append((residual / norm).astype(np.float32))
    return np.vstack(orthogonal).astype(np.float32)


def apply_direction(
    query_embedding: np.ndarray,
    direction: np.ndarray,
    strength: float,
) -> np.ndarray:
    """Move a normalized query along one axis and renormalize for cosine search."""
    if not np.isfinite(strength):
        raise ValueError("strength must be finite")
    query = _normalized_vector(query_embedding, "query_embedding")
    axis = _normalized_vector(direction, "direction")
    if query.shape != axis.shape:
        raise ValueError("query embedding and direction must have the same dimension")
    return _normalized_vector(query + float(strength) * axis, "adjusted query")


def _normalized_matrix(values: np.ndarray, name: str) -> np.ndarray:
    matrix = np.asarray(values, dtype=np.float32)
    if matrix.ndim != 2 or not matrix.shape[0] or not matrix.shape[1]:
        raise ValueError(f"{name} must be a non-empty 2D matrix")
    if not np.all(np.isfinite(matrix)):
        raise ValueError(f"{name} must contain only finite values")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms <= 1e-8):
        raise ValueError(f"{name} must not contain zero-length vectors")
    return (matrix / norms).astype(np.float32)


def _normalized_vector(value: np.ndarray, name: str) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float32)
    if vector.ndim != 1 or not vector.shape[0]:
        raise ValueError(f"{name} must be a non-empty 1D vector")
    if not np.all(np.isfinite(vector)):
        raise ValueError(f"{name} must contain only finite values")
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-8:
        raise ValueError(f"{name} must not be a zero-length vector")
    return (vector / norm).astype(np.float32)
