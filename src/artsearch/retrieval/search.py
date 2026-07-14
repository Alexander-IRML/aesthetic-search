from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from enum import Enum

import numpy as np

from artsearch.embed.storage import blob_to_matrix, blob_to_vector, l2_normalize
from artsearch.ingest.config import AppConfig


class RetrievalMode(str, Enum):
    CLIP_SUBJECT = "clip_subject"
    DINO_POOLED = "dino_pooled"
    DINO_PATCH_MAXSIM = "dino_patch_maxsim"

    @classmethod
    def coerce(cls, value: str | "RetrievalMode") -> "RetrievalMode":
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError as exc:
            valid = ", ".join(mode.value for mode in cls)
            raise ValueError(f"unsupported retrieval mode: {value!r}; expected one of {valid}") from exc

    @property
    def label(self) -> str:
        return RETRIEVAL_MODE_LABELS[self]


RETRIEVAL_MODE_LABELS = {
    RetrievalMode.CLIP_SUBJECT: "CLIP subject",
    RetrievalMode.DINO_POOLED: "DINO pooled",
    RetrievalMode.DINO_PATCH_MAXSIM: "DINO patch MaxSim",
}
SUPPORTED_RETRIEVAL_MODES = tuple(RetrievalMode)


@dataclass(frozen=True)
class SearchFilters:
    top_k: int | None = None
    include_same_artist: bool = False
    review_status: str | None = None
    is_sfw: bool | None = None


@dataclass(frozen=True)
class SearchResult:
    artwork_id: str
    artist_id: str
    artist_display_name: str
    processed_path: str
    score: float
    mode: RetrievalMode = RetrievalMode.DINO_POOLED
    review_status: str = "unreviewed"
    is_sfw: bool | None = None


@dataclass(frozen=True)
class SearchIndex:
    artwork_ids: list[str]
    artist_ids: list[str]
    artist_display_names: list[str]
    processed_paths: list[str]
    review_statuses: list[str]
    is_sfws: list[bool | None]
    vectors: np.ndarray | None = None
    patches: list[np.ndarray] | None = None


def search_similar(
    conn: sqlite3.Connection,
    config: AppConfig,
    query_artwork_id: str,
    *,
    top_k: int | None = None,
    mode: RetrievalMode | str = RetrievalMode.DINO_POOLED,
    filters: SearchFilters | None = None,
) -> list[SearchResult]:
    retrieval_mode = RetrievalMode.coerce(mode)
    search_filters = _resolve_filters(config, filters, top_k)
    index = load_search_index(conn, config, mode=retrieval_mode)
    if not index.artwork_ids:
        return []

    try:
        query_index = index.artwork_ids.index(query_artwork_id)
    except ValueError as exc:
        raise ValueError(f"artwork_id is not searchable: {query_artwork_id}") from exc

    scores = _score_index(index, query_index, retrieval_mode)
    ordered = np.argsort(-scores)

    results = []
    for index_position in ordered:
        row_index = int(index_position)
        if not _candidate_allowed(index, query_index, row_index, search_filters):
            continue
        results.append(
            SearchResult(
                artwork_id=index.artwork_ids[row_index],
                artist_id=index.artist_ids[row_index],
                artist_display_name=index.artist_display_names[row_index],
                processed_path=index.processed_paths[row_index],
                score=float(scores[row_index]),
                mode=retrieval_mode,
                review_status=index.review_statuses[row_index],
                is_sfw=index.is_sfws[row_index],
            )
        )
        if len(results) >= search_filters.top_k:
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
            artworks.processed_path,
            artworks.review_status,
            artworks.is_sfw
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
        review_status=row["review_status"],
        is_sfw=_bool_or_none(row["is_sfw"]),
    )


def load_search_index(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    mode: RetrievalMode | str = RetrievalMode.DINO_POOLED,
) -> SearchIndex:
    retrieval_mode = RetrievalMode.coerce(mode)
    if retrieval_mode in {RetrievalMode.CLIP_SUBJECT, RetrievalMode.DINO_POOLED}:
        return _load_vector_index(conn, config, retrieval_mode)
    return _load_patch_index(conn, config)


def patch_maxsim_score(query_patches: np.ndarray, candidate_patches: np.ndarray) -> float:
    query = np.asarray(query_patches, dtype=np.float32)
    candidate = np.asarray(candidate_patches, dtype=np.float32)
    if not len(query) or not len(candidate):
        return 0.0
    similarities = query @ candidate.T
    return float(np.max(similarities, axis=1).mean())


def _load_vector_index(
    conn: sqlite3.Connection,
    config: AppConfig,
    mode: RetrievalMode,
) -> SearchIndex:
    vector_column, dim_column = _vector_columns(mode)
    rows = conn.execute(
        f"""
        SELECT
            artworks.artwork_id,
            artworks.artist_id,
            artists.display_name AS artist_display_name,
            artworks.processed_path,
            artworks.review_status,
            artworks.is_sfw,
            embeddings.{vector_column} AS vector_blob,
            embeddings.{dim_column} AS vector_dim
          FROM artworks
          JOIN artists ON artists.artist_id = artworks.artist_id
          JOIN embeddings ON embeddings.artwork_id = artworks.artwork_id
         WHERE artworks.validated = 1
           AND artworks.processed_path IS NOT NULL
           AND embeddings.{vector_column} IS NOT NULL
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
    review_statuses = []
    is_sfws = []
    for row in rows:
        vector = blob_to_vector(row["vector_blob"], row["vector_dim"])
        vectors.append(l2_normalize(vector))
        artwork_ids.append(row["artwork_id"])
        artist_ids.append(row["artist_id"])
        artist_display_names.append(row["artist_display_name"])
        processed_paths.append(row["processed_path"])
        review_statuses.append(row["review_status"])
        is_sfws.append(_bool_or_none(row["is_sfw"]))

    matrix = np.vstack(vectors).astype(np.float32) if vectors else np.empty((0, 0))
    return SearchIndex(
        artwork_ids=artwork_ids,
        artist_ids=artist_ids,
        artist_display_names=artist_display_names,
        processed_paths=processed_paths,
        review_statuses=review_statuses,
        is_sfws=is_sfws,
        vectors=matrix,
    )


def _load_patch_index(conn: sqlite3.Connection, config: AppConfig) -> SearchIndex:
    rows = conn.execute(
        """
        SELECT
            artworks.artwork_id,
            artworks.artist_id,
            artists.display_name AS artist_display_name,
            artworks.processed_path,
            artworks.review_status,
            artworks.is_sfw,
            embeddings.dino_patches,
            embeddings.dino_patch_grid_size,
            embeddings.dino_patch_dim
          FROM artworks
          JOIN artists ON artists.artist_id = artworks.artist_id
          JOIN embeddings ON embeddings.artwork_id = artworks.artwork_id
         WHERE artworks.validated = 1
           AND artworks.processed_path IS NOT NULL
           AND embeddings.dino_patches IS NOT NULL
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

    patches = []
    artwork_ids = []
    artist_ids = []
    artist_display_names = []
    processed_paths = []
    review_statuses = []
    is_sfws = []
    for row in rows:
        patch_count = int(row["dino_patch_grid_size"]) ** 2
        patch_matrix = blob_to_matrix(row["dino_patches"], patch_count, row["dino_patch_dim"])
        patches.append(l2_normalize(patch_matrix, axis=1))
        artwork_ids.append(row["artwork_id"])
        artist_ids.append(row["artist_id"])
        artist_display_names.append(row["artist_display_name"])
        processed_paths.append(row["processed_path"])
        review_statuses.append(row["review_status"])
        is_sfws.append(_bool_or_none(row["is_sfw"]))

    return SearchIndex(
        artwork_ids=artwork_ids,
        artist_ids=artist_ids,
        artist_display_names=artist_display_names,
        processed_paths=processed_paths,
        review_statuses=review_statuses,
        is_sfws=is_sfws,
        patches=patches,
    )


def _vector_columns(mode: RetrievalMode) -> tuple[str, str]:
    if mode == RetrievalMode.CLIP_SUBJECT:
        return "clip_vector", "clip_dim"
    if mode == RetrievalMode.DINO_POOLED:
        return "dino_pooled", "dino_pooled_dim"
    raise ValueError(f"mode does not use vector columns: {mode.value}")


def _resolve_filters(
    config: AppConfig,
    filters: SearchFilters | None,
    top_k: int | None,
) -> SearchFilters:
    resolved = filters or SearchFilters()
    if top_k is not None:
        resolved = replace(resolved, top_k=top_k)
    if resolved.top_k is None:
        resolved = replace(resolved, top_k=config.retrieval.default_top_k)
    if resolved.top_k <= 0:
        raise ValueError("top_k must be positive")
    return resolved


def _score_index(
    index: SearchIndex,
    query_index: int,
    mode: RetrievalMode,
) -> np.ndarray:
    if mode in {RetrievalMode.CLIP_SUBJECT, RetrievalMode.DINO_POOLED}:
        if index.vectors is None:
            raise ValueError(f"search index has no vectors for mode: {mode.value}")
        query_vector = index.vectors[query_index]
        return index.vectors @ query_vector

    if index.patches is None:
        raise ValueError(f"search index has no patches for mode: {mode.value}")
    query_patches = index.patches[query_index]
    return np.array(
        [patch_maxsim_score(query_patches, candidate_patches) for candidate_patches in index.patches],
        dtype=np.float32,
    )


def _candidate_allowed(
    index: SearchIndex,
    query_index: int,
    row_index: int,
    filters: SearchFilters,
) -> bool:
    if row_index == query_index:
        return False
    if (
        not filters.include_same_artist
        and index.artist_ids[row_index] == index.artist_ids[query_index]
    ):
        return False
    if filters.review_status is not None and index.review_statuses[row_index] != filters.review_status:
        return False
    if filters.is_sfw is not None and index.is_sfws[row_index] != filters.is_sfw:
        return False
    return True


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)
