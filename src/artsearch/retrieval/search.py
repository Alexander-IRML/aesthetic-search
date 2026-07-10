from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from artsearch.embed.storage import blob_to_vector, l2_normalize
from artsearch.ingest.config import AppConfig


@dataclass(frozen=True)
class SearchResult:
    artwork_id: str
    artist_id: str
    artist_display_name: str
    processed_path: str
    score: float


@dataclass(frozen=True)
class SearchIndex:
    artwork_ids: list[str]
    artist_ids: list[str]
    artist_display_names: list[str]
    processed_paths: list[str]
    vectors: np.ndarray


def search_similar(
    conn: sqlite3.Connection,
    config: AppConfig,
    query_artwork_id: str,
    *,
    top_k: int | None = None,
) -> list[SearchResult]:
    limit = top_k or config.retrieval.default_top_k
    index = load_search_index(conn, config)
    if not index.artwork_ids:
        return []

    try:
        query_index = index.artwork_ids.index(query_artwork_id)
    except ValueError as exc:
        raise ValueError(f"artwork_id is not searchable: {query_artwork_id}") from exc

    query_artist_id = index.artist_ids[query_index]
    query_vector = index.vectors[query_index]
    scores = index.vectors @ query_vector
    ordered = np.argsort(-scores)

    results = []
    for index_position in ordered:
        row_index = int(index_position)
        artwork_id = index.artwork_ids[row_index]
        if artwork_id == query_artwork_id:
            continue
        if index.artist_ids[row_index] == query_artist_id:
            continue
        results.append(
            SearchResult(
                artwork_id=artwork_id,
                artist_id=index.artist_ids[row_index],
                artist_display_name=index.artist_display_names[row_index],
                processed_path=index.processed_paths[row_index],
                score=float(scores[row_index]),
            )
        )
        if len(results) >= limit:
            break

    return results


def get_artwork_for_demo(
    conn: sqlite3.Connection,
    config: AppConfig,
    artwork_id: str,
) -> SearchResult:
    row = conn.execute(
        """
        SELECT
            artworks.artwork_id,
            artworks.artist_id,
            artists.display_name AS artist_display_name,
            artworks.processed_path
          FROM artworks
          JOIN artists ON artists.artist_id = artworks.artist_id
         WHERE artworks.artwork_id = ?
           AND artworks.validated = 1
           AND artworks.processed_path IS NOT NULL
        """,
        (artwork_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"validated artwork not found: {artwork_id}")
    return SearchResult(
        artwork_id=row["artwork_id"],
        artist_id=row["artist_id"],
        artist_display_name=row["artist_display_name"],
        processed_path=row["processed_path"],
        score=1.0,
    )


def load_search_index(conn: sqlite3.Connection, config: AppConfig) -> SearchIndex:
    rows = conn.execute(
        """
        SELECT
            artworks.artwork_id,
            artworks.artist_id,
            artists.display_name AS artist_display_name,
            artworks.processed_path,
            embeddings.dino_pooled,
            embeddings.dino_pooled_dim
          FROM artworks
          JOIN artists ON artists.artist_id = artworks.artist_id
          JOIN embeddings ON embeddings.artwork_id = artworks.artwork_id
         WHERE artworks.validated = 1
           AND artworks.processed_path IS NOT NULL
           AND embeddings.model_name_dino = ?
           AND embeddings.model_version_dino = ?
           AND embeddings.model_name_clip = ?
           AND embeddings.model_version_clip = ?
         ORDER BY artworks.artwork_id
        """,
        (
            config.models.dino_model_name,
            config.models.dino_model_version,
            config.models.clip_model_name,
            config.models.clip_model_version,
        ),
    ).fetchall()

    vectors = []
    artwork_ids = []
    artist_ids = []
    artist_display_names = []
    processed_paths = []
    for row in rows:
        vector = blob_to_vector(row["dino_pooled"], row["dino_pooled_dim"])
        vectors.append(l2_normalize(vector))
        artwork_ids.append(row["artwork_id"])
        artist_ids.append(row["artist_id"])
        artist_display_names.append(row["artist_display_name"])
        processed_paths.append(row["processed_path"])

    matrix = np.vstack(vectors).astype(np.float32) if vectors else np.empty((0, 0))
    return SearchIndex(
        artwork_ids=artwork_ids,
        artist_ids=artist_ids,
        artist_display_names=artist_display_names,
        processed_paths=processed_paths,
        vectors=matrix,
    )
