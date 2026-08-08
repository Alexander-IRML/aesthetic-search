from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from enum import Enum

import numpy as np

from artsearch.embed.storage import blob_to_matrix, blob_to_vector, l2_normalize
from artsearch.ingest.config import AppConfig


class RetrievalMode(str, Enum):
    ENSEMBLE = "ensemble"
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
    RetrievalMode.ENSEMBLE: "Ensemble: global shortlist + local rerank",
    RetrievalMode.CLIP_SUBJECT: "CLIP subject",
    RetrievalMode.DINO_POOLED: "DINO pooled: style/global",
    RetrievalMode.DINO_PATCH_MAXSIM: "DINO patch MaxSim: local detail",
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
    mode: RetrievalMode = RetrievalMode.ENSEMBLE
    review_status: str = "unreviewed"
    is_sfw: bool | None = None
    pooled_score: float | None = None
    pooled_rank: int | None = None
    patch_score: float | None = None
    patch_rank: int | None = None
    clip_score: float | None = None
    clip_rank: int | None = None
    shortlist_size: int | None = None
    candidate_count: int | None = None
    patch_match_top_n: int | None = None


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
    clip_vectors: np.ndarray | None = None
    pooled_vectors: np.ndarray | None = None


def search_similar(
    conn: sqlite3.Connection,
    config: AppConfig,
    query_artwork_id: str,
    *,
    top_k: int | None = None,
    mode: RetrievalMode | str = RetrievalMode.ENSEMBLE,
    filters: SearchFilters | None = None,
) -> list[SearchResult]:
    retrieval_mode = RetrievalMode.coerce(mode)
    search_filters = _resolve_filters(config, filters, top_k)
    if retrieval_mode == RetrievalMode.ENSEMBLE:
        return _search_ensemble(
            conn,
            config,
            query_artwork_id,
            search_filters,
        )

    index = load_search_index(conn, config, mode=retrieval_mode)
    if not index.artwork_ids:
        return []

    try:
        query_index = index.artwork_ids.index(query_artwork_id)
    except ValueError as exc:
        raise ValueError(f"artwork_id is not searchable: {query_artwork_id}") from exc

    scores = _score_index(
        index,
        query_index,
        retrieval_mode,
        patch_match_top_n=config.retrieval.patch_match_top_n,
    )
    ordered = np.argsort(-scores, kind="stable")
    candidate_count = sum(
        _candidate_allowed(index, query_index, candidate, search_filters)
        for candidate in range(len(index.artwork_ids))
    )

    results = []
    candidate_rank = 0
    for index_position in ordered:
        row_index = int(index_position)
        if not _candidate_allowed(index, query_index, row_index, search_filters):
            continue
        candidate_rank += 1
        component_values = _standalone_component_values(
            retrieval_mode,
            float(scores[row_index]),
            candidate_rank,
        )
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
                candidate_count=candidate_count,
                patch_match_top_n=(
                    config.retrieval.patch_match_top_n
                    if retrieval_mode == RetrievalMode.DINO_PATCH_MAXSIM
                    else None
                ),
                **component_values,
            )
        )
        if len(results) >= search_filters.top_k:
            break

    return results


def _search_ensemble(
    conn: sqlite3.Connection,
    config: AppConfig,
    query_artwork_id: str,
    filters: SearchFilters,
) -> list[SearchResult]:
    """Run broad DINO recall before local reranking; CLIP remains diagnostic."""
    index = _load_ensemble_recall_index(conn, config)
    if not index.artwork_ids:
        return []

    try:
        query_index = index.artwork_ids.index(query_artwork_id)
    except ValueError as exc:
        raise ValueError(
            f"artwork_id is not searchable by the ensemble: {query_artwork_id}"
        ) from exc

    if index.pooled_vectors is None or index.clip_vectors is None:
        raise ValueError("ensemble recall index is missing one or more global vectors")

    candidate_indices = [
        row_index
        for row_index in range(len(index.artwork_ids))
        if _candidate_allowed(index, query_index, row_index, filters)
    ]
    if not candidate_indices:
        return []

    pooled_scores = index.pooled_vectors @ index.pooled_vectors[query_index]
    clip_scores = index.clip_vectors @ index.clip_vectors[query_index]
    pooled_order = _ordered_candidates(candidate_indices, pooled_scores, index.artwork_ids)
    clip_order = _ordered_candidates(candidate_indices, clip_scores, index.artwork_ids)
    pooled_ranks = _rank_map(pooled_order)
    clip_ranks = _rank_map(clip_order)

    requested_shortlist = max(filters.top_k, config.retrieval.shortlist_size)
    shortlist = pooled_order[:requested_shortlist]
    patch_artwork_ids = [
        query_artwork_id,
        *(index.artwork_ids[row_index] for row_index in shortlist),
    ]
    patches_by_artwork = _load_patch_matrices(conn, config, patch_artwork_ids)
    missing_patches = set(patch_artwork_ids) - patches_by_artwork.keys()
    if missing_patches:
        missing = ", ".join(sorted(missing_patches))
        raise ValueError(f"ensemble shortlist has missing patch embeddings: {missing}")
    query_patches = patches_by_artwork[query_artwork_id]
    patch_scores = {
        row_index: patch_maxsim_score(
            query_patches,
            patches_by_artwork[index.artwork_ids[row_index]],
            top_n=config.retrieval.patch_match_top_n,
        )
        for row_index in shortlist
    }
    patch_order = sorted(
        shortlist,
        key=lambda row_index: (
            -patch_scores[row_index],
            -float(pooled_scores[row_index]),
            index.artwork_ids[row_index],
        ),
    )
    patch_ranks = _rank_map(patch_order)

    results = []
    for row_index in patch_order[: filters.top_k]:
        results.append(
            SearchResult(
                artwork_id=index.artwork_ids[row_index],
                artist_id=index.artist_ids[row_index],
                artist_display_name=index.artist_display_names[row_index],
                processed_path=index.processed_paths[row_index],
                score=patch_scores[row_index],
                mode=RetrievalMode.ENSEMBLE,
                review_status=index.review_statuses[row_index],
                is_sfw=index.is_sfws[row_index],
                pooled_score=float(pooled_scores[row_index]),
                pooled_rank=pooled_ranks[row_index],
                patch_score=patch_scores[row_index],
                patch_rank=patch_ranks[row_index],
                clip_score=float(clip_scores[row_index]),
                clip_rank=clip_ranks[row_index],
                shortlist_size=len(shortlist),
                candidate_count=len(candidate_indices),
                patch_match_top_n=config.retrieval.patch_match_top_n,
            )
        )
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
    mode: RetrievalMode | str = RetrievalMode.ENSEMBLE,
) -> SearchIndex:
    retrieval_mode = RetrievalMode.coerce(mode)
    if retrieval_mode == RetrievalMode.ENSEMBLE:
        return _load_ensemble_recall_index(conn, config)
    if retrieval_mode in {RetrievalMode.CLIP_SUBJECT, RetrievalMode.DINO_POOLED}:
        return _load_vector_index(conn, config, retrieval_mode)
    return _load_patch_index(conn, config)


def patch_maxsim_score(
    query_patches: np.ndarray,
    candidate_patches: np.ndarray,
    *,
    top_n: int = 1,
) -> float:
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    query = np.asarray(query_patches, dtype=np.float32)
    candidate = np.asarray(candidate_patches, dtype=np.float32)
    if not len(query) or not len(candidate):
        return 0.0
    similarities = query @ candidate.T
    match_count = min(top_n, similarities.shape[1])
    if match_count == 1:
        per_query_patch = np.max(similarities, axis=1)
    else:
        split = similarities.shape[1] - match_count
        top_matches = np.partition(similarities, split, axis=1)[:, split:]
        per_query_patch = top_matches.mean(axis=1)
    return float(per_query_patch.mean())


def _load_ensemble_recall_index(
    conn: sqlite3.Connection,
    config: AppConfig,
) -> SearchIndex:
    rows = conn.execute(
        """
        SELECT
            artworks.artwork_id,
            artworks.artist_id,
            artists.display_name AS artist_display_name,
            artworks.processed_path,
            artworks.review_status,
            artworks.is_sfw,
            embeddings.clip_vector,
            embeddings.clip_dim,
            embeddings.dino_pooled,
            embeddings.dino_pooled_dim
          FROM artworks
          JOIN artists ON artists.artist_id = artworks.artist_id
          JOIN embeddings ON embeddings.artwork_id = artworks.artwork_id
         WHERE artworks.validated = 1
           AND artworks.processed_path IS NOT NULL
           AND embeddings.clip_vector IS NOT NULL
           AND embeddings.dino_pooled IS NOT NULL
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

    clip_vectors = []
    pooled_vectors = []
    artwork_ids = []
    artist_ids = []
    artist_display_names = []
    processed_paths = []
    review_statuses = []
    is_sfws = []
    for row in rows:
        clip_vectors.append(
            l2_normalize(blob_to_vector(row["clip_vector"], row["clip_dim"]))
        )
        pooled_vectors.append(
            l2_normalize(blob_to_vector(row["dino_pooled"], row["dino_pooled_dim"]))
        )
        artwork_ids.append(row["artwork_id"])
        artist_ids.append(row["artist_id"])
        artist_display_names.append(row["artist_display_name"])
        processed_paths.append(row["processed_path"])
        review_statuses.append(row["review_status"])
        is_sfws.append(_bool_or_none(row["is_sfw"]))

    clip_matrix = (
        np.vstack(clip_vectors).astype(np.float32) if clip_vectors else np.empty((0, 0))
    )
    pooled_matrix = (
        np.vstack(pooled_vectors).astype(np.float32)
        if pooled_vectors
        else np.empty((0, 0))
    )
    return SearchIndex(
        artwork_ids=artwork_ids,
        artist_ids=artist_ids,
        artist_display_names=artist_display_names,
        processed_paths=processed_paths,
        review_statuses=review_statuses,
        is_sfws=is_sfws,
        clip_vectors=clip_matrix,
        pooled_vectors=pooled_matrix,
    )


def _load_patch_matrices(
    conn: sqlite3.Connection,
    config: AppConfig,
    artwork_ids: list[str],
) -> dict[str, np.ndarray]:
    if not artwork_ids:
        return {}
    unique_ids = list(dict.fromkeys(artwork_ids))
    placeholders = ", ".join("?" for _ in unique_ids)
    rows = conn.execute(
        f"""
        SELECT
            embeddings.artwork_id,
            embeddings.dino_patches,
            embeddings.dino_patch_grid_size,
            embeddings.dino_patch_dim
          FROM embeddings
         WHERE embeddings.artwork_id IN ({placeholders})
           AND embeddings.dino_patches IS NOT NULL
           AND embeddings.model_name_dino = ?
           AND embeddings.model_version_dino = ?
           AND embeddings.model_name_clip = ?
           AND embeddings.model_version_clip = ?
        """,
        (
            *unique_ids,
            config.models.dino_model_name,
            config.models.dino_model_version,
            config.models.clip_model_name,
            config.models.clip_model_version,
        ),
    ).fetchall()
    matrices = {}
    for row in rows:
        patch_count = int(row["dino_patch_grid_size"]) ** 2
        patches = blob_to_matrix(
            row["dino_patches"],
            patch_count,
            row["dino_patch_dim"],
        )
        matrices[row["artwork_id"]] = l2_normalize(patches, axis=1)
    return matrices


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
    *,
    patch_match_top_n: int = 1,
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
        [
            patch_maxsim_score(
                query_patches,
                candidate_patches,
                top_n=patch_match_top_n,
            )
            for candidate_patches in index.patches
        ],
        dtype=np.float32,
    )


def _ordered_candidates(
    candidate_indices: list[int],
    scores: np.ndarray,
    artwork_ids: list[str],
) -> list[int]:
    return sorted(
        candidate_indices,
        key=lambda row_index: (-float(scores[row_index]), artwork_ids[row_index]),
    )


def _rank_map(ordered_indices: list[int]) -> dict[int, int]:
    return {
        row_index: rank
        for rank, row_index in enumerate(ordered_indices, start=1)
    }


def _standalone_component_values(
    mode: RetrievalMode,
    score: float,
    rank: int,
) -> dict[str, float | int | None]:
    if mode == RetrievalMode.CLIP_SUBJECT:
        return {"clip_score": score, "clip_rank": rank}
    if mode == RetrievalMode.DINO_POOLED:
        return {"pooled_score": score, "pooled_rank": rank}
    if mode == RetrievalMode.DINO_PATCH_MAXSIM:
        return {"patch_score": score, "patch_rank": rank}
    raise ValueError(f"mode is not a standalone retrieval signal: {mode.value}")


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
