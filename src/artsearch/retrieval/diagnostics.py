from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from artsearch.embed.storage import blob_to_matrix, l2_normalize
from artsearch.ingest.config import AppConfig


@dataclass(frozen=True)
class PatchMatch:
    query_patch_index: int
    candidate_patch_index: int
    query_row: int
    query_col: int
    candidate_row: int
    candidate_col: int
    score: float


def patch_maxsim_diagnostics(
    conn: sqlite3.Connection,
    config: AppConfig,
    query_artwork_id: str,
    candidate_artwork_id: str,
    *,
    top_n: int = 20,
) -> list[PatchMatch]:
    if top_n <= 0:
        raise ValueError("top_n must be positive")

    query_patches, query_grid_size = load_dino_patch_matrix(conn, config, query_artwork_id)
    candidate_patches, candidate_grid_size = load_dino_patch_matrix(
        conn,
        config,
        candidate_artwork_id,
    )
    similarities = query_patches @ candidate_patches.T
    best_candidate_indices = np.argmax(similarities, axis=1)
    best_scores = similarities[np.arange(similarities.shape[0]), best_candidate_indices]
    ordered_query_indices = np.argsort(-best_scores)[:top_n]

    matches = []
    for query_index in ordered_query_indices:
        candidate_index = int(best_candidate_indices[query_index])
        matches.append(
            PatchMatch(
                query_patch_index=int(query_index),
                candidate_patch_index=candidate_index,
                query_row=int(query_index) // query_grid_size,
                query_col=int(query_index) % query_grid_size,
                candidate_row=candidate_index // candidate_grid_size,
                candidate_col=candidate_index % candidate_grid_size,
                score=float(best_scores[query_index]),
            )
        )
    return matches


def load_dino_patch_matrix(
    conn: sqlite3.Connection,
    config: AppConfig,
    artwork_id: str,
) -> tuple[np.ndarray, int]:
    row = conn.execute(
        """
        SELECT
            embeddings.dino_patches,
            embeddings.dino_patch_grid_size,
            embeddings.dino_patch_dim
          FROM artworks
          JOIN embeddings ON embeddings.artwork_id = artworks.artwork_id
         WHERE artworks.artwork_id = ?
           AND artworks.validated = 1
           AND artworks.processed_path IS NOT NULL
           AND embeddings.dino_patches IS NOT NULL
           AND embeddings.model_name_dino = ?
           AND embeddings.model_version_dino = ?
           AND embeddings.model_name_clip = ?
           AND embeddings.model_version_clip = ?
        """,
        (
            artwork_id,
            config.models.dino_model_name,
            config.models.dino_model_version,
            config.models.clip_model_name,
            config.models.clip_model_version,
        ),
    ).fetchone()
    if row is None:
        raise ValueError(f"artwork_id has no current DINO patch embeddings: {artwork_id}")

    grid_size = int(row["dino_patch_grid_size"])
    patch_count = grid_size**2
    patches = blob_to_matrix(row["dino_patches"], patch_count, row["dino_patch_dim"])
    return l2_normalize(patches, axis=1), grid_size
