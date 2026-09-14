from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from typing import Literal, Protocol, Sequence

import numpy as np


@dataclass(frozen=True)
class SearchHit:
    """Backend-neutral retrieval evidence consumed by the API and telemetry."""

    artwork_id: str
    artist_id: str
    score: float
    rank: int
    content_class: str = "unknown"
    fusion_score: float | None = None
    fusion_rank: int | None = None
    clip_score: float | None = None
    clip_rank: int | None = None
    style_score: float | None = None
    style_rank: int | None = None

    def to_dict(self) -> dict[str, object]:
        from dataclasses import asdict

        return asdict(self)


class VectorSearchBackend(Protocol):
    """Minimal online contract shared by Qdrant and immutable FAISS snapshots."""

    def search_by_vectors(
        self,
        clip_vector: np.ndarray,
        style_vector: np.ndarray,
        *,
        top_k: int = 10,
        artist_id: str | None = None,
    ) -> Sequence[SearchHit]: ...


class AsyncVectorSearchBackend(Protocol):
    """Nonblocking serving contract for remote or executor-isolated retrieval."""

    async def search_by_vectors(
        self,
        clip_vector: np.ndarray,
        style_vector: np.ndarray,
        *,
        top_k: int = 10,
        artist_id: str | None = None,
    ) -> Sequence[SearchHit]: ...


class ThreadedVectorSearchBackend:
    """Keep a synchronous local backend off the ASGI event loop."""

    def __init__(self, backend: VectorSearchBackend) -> None:
        self.backend = backend

    async def search_by_vectors(
        self,
        clip_vector: np.ndarray,
        style_vector: np.ndarray,
        *,
        top_k: int = 10,
        artist_id: str | None = None,
    ) -> Sequence[SearchHit]:
        operation = partial(
            self.backend.search_by_vectors,
            clip_vector,
            style_vector,
            top_k=top_k,
            artist_id=artist_id,
        )
        return await asyncio.to_thread(operation)


@dataclass(frozen=True)
class RetrievalContract:
    """Version and limit evidence that accompanies every served result page."""

    backend: Literal["qdrant", "faiss"]
    model_bundle_version: str
    index_alias: str
    index_version: str
    candidate_limit: int
    clip_dimension: int
    csd_dimension: int
