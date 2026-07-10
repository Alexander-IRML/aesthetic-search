from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from artsearch.ingest.config import ModelConfig


@dataclass(frozen=True)
class ImageEmbeddings:
    clip_vector: np.ndarray
    dino_pooled: np.ndarray
    dino_patches: np.ndarray
    dino_patch_grid_size: int

    @property
    def clip_dim(self) -> int:
        return int(self.clip_vector.shape[0])

    @property
    def dino_pooled_dim(self) -> int:
        return int(self.dino_pooled.shape[0])

    @property
    def dino_patch_dim(self) -> int:
        return int(self.dino_patches.shape[-1])


def l2_normalize(values: np.ndarray, *, axis: int | None = None) -> np.ndarray:
    array = np.asarray(values, dtype=np.float32)
    norm = np.linalg.norm(array, axis=axis, keepdims=axis is not None)
    if axis is None:
        if norm == 0:
            return np.zeros_like(array, dtype=np.float32)
        return (array / norm).astype(np.float32)
    return np.divide(array, norm, out=np.zeros_like(array), where=norm > 0).astype(np.float32)


def normalize_embeddings(embeddings: ImageEmbeddings) -> ImageEmbeddings:
    return ImageEmbeddings(
        clip_vector=l2_normalize(embeddings.clip_vector),
        dino_pooled=l2_normalize(embeddings.dino_pooled),
        dino_patches=l2_normalize(embeddings.dino_patches, axis=1),
        dino_patch_grid_size=embeddings.dino_patch_grid_size,
    )


def array_to_blob(values: np.ndarray) -> bytes:
    return np.ascontiguousarray(values, dtype=np.float32).tobytes()


def blob_to_vector(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).reshape(dim)


def blob_to_matrix(blob: bytes, rows: int, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).reshape(rows, dim)


def model_versions_match(row: sqlite3.Row | None, models: ModelConfig) -> bool:
    if row is None:
        return False
    return (
        row["model_name_clip"] == models.clip_model_name
        and row["model_version_clip"] == models.clip_model_version
        and row["model_name_dino"] == models.dino_model_name
        and row["model_version_dino"] == models.dino_model_version
    )


def find_embedding(conn: sqlite3.Connection, artwork_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM embeddings WHERE artwork_id = ?",
        (artwork_id,),
    ).fetchone()


def upsert_embedding(
    conn: sqlite3.Connection,
    artwork_id: str,
    embeddings: ImageEmbeddings,
    models: ModelConfig,
) -> None:
    normalized = normalize_embeddings(embeddings)
    conn.execute(
        """
        INSERT INTO embeddings (
            artwork_id,
            clip_vector,
            clip_dim,
            dino_pooled,
            dino_pooled_dim,
            dino_patches,
            dino_patch_grid_size,
            dino_patch_dim,
            model_name_clip,
            model_version_clip,
            model_name_dino,
            model_version_dino,
            date_computed
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(artwork_id) DO UPDATE SET
            clip_vector = excluded.clip_vector,
            clip_dim = excluded.clip_dim,
            dino_pooled = excluded.dino_pooled,
            dino_pooled_dim = excluded.dino_pooled_dim,
            dino_patches = excluded.dino_patches,
            dino_patch_grid_size = excluded.dino_patch_grid_size,
            dino_patch_dim = excluded.dino_patch_dim,
            model_name_clip = excluded.model_name_clip,
            model_version_clip = excluded.model_version_clip,
            model_name_dino = excluded.model_name_dino,
            model_version_dino = excluded.model_version_dino,
            date_computed = CURRENT_TIMESTAMP
        """,
        (
            artwork_id,
            array_to_blob(normalized.clip_vector),
            normalized.clip_dim,
            array_to_blob(normalized.dino_pooled),
            normalized.dino_pooled_dim,
            array_to_blob(normalized.dino_patches),
            normalized.dino_patch_grid_size,
            normalized.dino_patch_dim,
            models.clip_model_name,
            models.clip_model_version,
            models.dino_model_name,
            models.dino_model_version,
        ),
    )
    conn.commit()
