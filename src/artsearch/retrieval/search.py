from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from enum import Enum

import numpy as np

from artsearch.embed.storage import blob_to_vector, l2_normalize
from artsearch.ingest.config import AppConfig


class RetrievalMode(str, Enum):
    ENSEMBLE = "ensemble"
    CLIP_SUBJECT = "clip_subject"
    CSD_STYLE = "csd_style"

    # Kept only so append-only v0.3 judgment files remain readable.
    LEGACY_DINO_POOLED = "dino_pooled"
    LEGACY_DINO_PATCH_MAXSIM = "dino_patch_maxsim"

    @classmethod
    def coerce(cls, value: str | "RetrievalMode") -> "RetrievalMode":
        mode = value if isinstance(value, cls) else cls(value)
        if mode not in SUPPORTED_RETRIEVAL_MODES:
            valid = ", ".join(item.value for item in SUPPORTED_RETRIEVAL_MODES)
            raise ValueError(f"unsupported active retrieval mode: {mode.value!r}; expected {valid}")
        return mode

    @property
    def label(self) -> str:
        return RETRIEVAL_MODE_LABELS[self]


RETRIEVAL_MODE_LABELS = {
    RetrievalMode.ENSEMBLE: "Ensemble: CLIP subject + CSD style",
    RetrievalMode.CLIP_SUBJECT: "CLIP subject",
    RetrievalMode.CSD_STYLE: "CSD style",
    RetrievalMode.LEGACY_DINO_POOLED: "Legacy DINO pooled",
    RetrievalMode.LEGACY_DINO_PATCH_MAXSIM: "Legacy DINO patch MaxSim",
}
SUPPORTED_RETRIEVAL_MODES = (
    RetrievalMode.ENSEMBLE,
    RetrievalMode.CLIP_SUBJECT,
    RetrievalMode.CSD_STYLE,
)


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
    fusion_score: float | None = None
    fusion_rank: int | None = None
    clip_score: float | None = None
    clip_rank: int | None = None
    style_score: float | None = None
    style_rank: int | None = None
    shortlist_size: int | None = None
    candidate_count: int | None = None


@dataclass(frozen=True)
class SearchIndex:
    artwork_ids: list[str]
    artist_ids: list[str]
    artist_display_names: list[str]
    processed_paths: list[str]
    review_statuses: list[str]
    is_sfws: list[bool | None]
    vectors: np.ndarray | None = None
    clip_vectors: np.ndarray | None = None
    style_vectors: np.ndarray | None = None


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
        return _search_ensemble(conn, config, query_artwork_id, search_filters)

    index = load_search_index(conn, config, mode=retrieval_mode)
    if not index.artwork_ids:
        return []
    query_index = _query_index(index, query_artwork_id, retrieval_mode)
    if index.vectors is None:
        raise ValueError(f"search index has no vectors for mode: {retrieval_mode.value}")
    scores = index.vectors @ index.vectors[query_index]
    ordered = _ordered_candidates(
        [
            row_index
            for row_index in range(len(index.artwork_ids))
            if _candidate_allowed(index, query_index, row_index, search_filters)
        ],
        scores,
        index.artwork_ids,
    )
    results: list[SearchResult] = []
    for rank, row_index in enumerate(ordered[: search_filters.top_k], start=1):
        score = float(scores[row_index])
        evidence = (
            {"clip_score": score, "clip_rank": rank}
            if retrieval_mode == RetrievalMode.CLIP_SUBJECT
            else {"style_score": score, "style_rank": rank}
        )
        results.append(
            SearchResult(
                artwork_id=index.artwork_ids[row_index],
                artist_id=index.artist_ids[row_index],
                artist_display_name=index.artist_display_names[row_index],
                processed_path=index.processed_paths[row_index],
                score=score,
                mode=retrieval_mode,
                review_status=index.review_statuses[row_index],
                is_sfw=index.is_sfws[row_index],
                candidate_count=len(ordered),
                **evidence,
            )
        )
    return results


def _search_ensemble(
    conn: sqlite3.Connection,
    config: AppConfig,
    query_artwork_id: str,
    filters: SearchFilters,
) -> list[SearchResult]:
    """Fuse independent semantic and style rankings with reciprocal rank fusion."""

    index = _load_ensemble_index(conn, config)
    if not index.artwork_ids:
        return []
    query_index = _query_index(index, query_artwork_id, RetrievalMode.ENSEMBLE)
    if index.clip_vectors is None or index.style_vectors is None:
        raise ValueError("ensemble index is missing CLIP or CSD vectors")

    candidates = [
        row_index
        for row_index in range(len(index.artwork_ids))
        if _candidate_allowed(index, query_index, row_index, filters)
    ]
    if not candidates:
        return []

    clip_scores = index.clip_vectors @ index.clip_vectors[query_index]
    style_scores = index.style_vectors @ index.style_vectors[query_index]
    clip_order = _ordered_candidates(candidates, clip_scores, index.artwork_ids)
    style_order = _ordered_candidates(candidates, style_scores, index.artwork_ids)
    clip_ranks = _rank_map(clip_order)
    style_ranks = _rank_map(style_order)
    recall_depth = min(len(candidates), max(filters.top_k, config.retrieval.shortlist_size))
    clip_recall = set(clip_order[:recall_depth])
    style_recall = set(style_order[:recall_depth])
    shortlist = clip_recall | style_recall
    fusion_scores = {
        row_index: (
            (_rrf(clip_ranks[row_index]) if row_index in clip_recall else 0.0)
            + (_rrf(style_ranks[row_index]) if row_index in style_recall else 0.0)
        )
        for row_index in shortlist
    }
    fused_order = sorted(
        shortlist,
        key=lambda row_index: (
            -fusion_scores[row_index],
            -float(style_scores[row_index]),
            -float(clip_scores[row_index]),
            index.artwork_ids[row_index],
        ),
    )

    return [
        SearchResult(
            artwork_id=index.artwork_ids[row_index],
            artist_id=index.artist_ids[row_index],
            artist_display_name=index.artist_display_names[row_index],
            processed_path=index.processed_paths[row_index],
            score=fusion_scores[row_index],
            mode=RetrievalMode.ENSEMBLE,
            review_status=index.review_statuses[row_index],
            is_sfw=index.is_sfws[row_index],
            fusion_score=fusion_scores[row_index],
            fusion_rank=rank,
            clip_score=float(clip_scores[row_index]),
            clip_rank=clip_ranks[row_index],
            style_score=float(style_scores[row_index]),
            style_rank=style_ranks[row_index],
            shortlist_size=len(shortlist),
            candidate_count=len(candidates),
        )
        for rank, row_index in enumerate(fused_order[: filters.top_k], start=1)
    ]


def get_artwork_for_demo(
    conn: sqlite3.Connection,
    config: AppConfig,
    artwork_id: str,
) -> SearchResult:
    row = conn.execute(
        """
        SELECT a.artwork_id, a.artist_id, artists.display_name AS artist_display_name,
               a.processed_path, a.review_status, a.is_sfw
          FROM artworks AS a
          JOIN artists ON artists.artist_id = a.artist_id
         WHERE a.artwork_id = ? AND a.validated = 1 AND a.processed_path IS NOT NULL
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
        return _load_ensemble_index(conn, config)
    return _load_vector_index(conn, config, retrieval_mode)


def _load_ensemble_index(conn: sqlite3.Connection, config: AppConfig) -> SearchIndex:
    rows = _embedding_rows(conn, config)
    return SearchIndex(
        **_index_metadata(rows),
        clip_vectors=_matrix(rows, "clip_vector", "clip_dim"),
        style_vectors=_matrix(rows, "csd_style", "csd_style_dim"),
    )


def _load_vector_index(
    conn: sqlite3.Connection,
    config: AppConfig,
    mode: RetrievalMode,
) -> SearchIndex:
    rows = _embedding_rows(conn, config)
    vector_column, dim_column = _vector_columns(mode)
    return SearchIndex(
        **_index_metadata(rows),
        vectors=_matrix(rows, vector_column, dim_column),
    )


def _embedding_rows(conn: sqlite3.Connection, config: AppConfig) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.artwork_id, a.artist_id, artists.display_name AS artist_display_name,
               a.processed_path, a.review_status, a.is_sfw,
               e.clip_vector, e.clip_dim, e.csd_style, e.csd_style_dim
          FROM artworks AS a
          JOIN artists ON artists.artist_id = a.artist_id
          JOIN embeddings AS e ON e.artwork_id = a.artwork_id
         WHERE a.validated = 1
           AND a.processed_path IS NOT NULL
           AND e.clip_vector IS NOT NULL
           AND e.csd_style IS NOT NULL
           AND e.model_name_clip = ?
           AND e.model_version_clip = ?
           AND e.model_name_csd = ?
           AND e.model_version_csd = ?
         ORDER BY a.artwork_id
        """,
        (
            config.models.clip_model_name,
            config.models.clip_model_version,
            config.models.csd_model_name,
            config.models.csd_model_version,
        ),
    ).fetchall()


def _index_metadata(rows: list[sqlite3.Row]) -> dict[str, list]:
    return {
        "artwork_ids": [str(row["artwork_id"]) for row in rows],
        "artist_ids": [str(row["artist_id"]) for row in rows],
        "artist_display_names": [str(row["artist_display_name"]) for row in rows],
        "processed_paths": [str(row["processed_path"]) for row in rows],
        "review_statuses": [str(row["review_status"]) for row in rows],
        "is_sfws": [_bool_or_none(row["is_sfw"]) for row in rows],
    }


def _matrix(rows: list[sqlite3.Row], blob_column: str, dim_column: str) -> np.ndarray:
    vectors = [l2_normalize(blob_to_vector(row[blob_column], int(row[dim_column]))) for row in rows]
    return np.vstack(vectors).astype(np.float32) if vectors else np.empty((0, 0))


def _vector_columns(mode: RetrievalMode) -> tuple[str, str]:
    if mode == RetrievalMode.CLIP_SUBJECT:
        return "clip_vector", "clip_dim"
    if mode == RetrievalMode.CSD_STYLE:
        return "csd_style", "csd_style_dim"
    raise ValueError(f"mode does not use a current vector: {mode.value}")


def _query_index(index: SearchIndex, artwork_id: str, mode: RetrievalMode) -> int:
    try:
        return index.artwork_ids.index(artwork_id)
    except ValueError as exc:
        raise ValueError(f"artwork_id is not searchable by {mode.value}: {artwork_id}") from exc


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
    return {row_index: rank for rank, row_index in enumerate(ordered_indices, start=1)}


def _rrf(rank: int, *, rank_constant: int = 60) -> float:
    return 1.0 / (rank_constant + rank)


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
    if (
        filters.review_status is not None
        and index.review_statuses[row_index] != filters.review_status
    ):
        return False
    if filters.is_sfw is not None and index.is_sfws[row_index] != filters.is_sfw:
        return False
    return True


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)
