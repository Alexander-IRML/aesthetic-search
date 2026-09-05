from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Protocol

from PIL import Image

from artsearch.embed.models import HuggingFaceEmbeddingProvider
from artsearch.embed.storage import ImageEmbeddings, normalize_embeddings
from artsearch.ingest.config import AppConfig
from artsearch.production.config import QdrantConfig


@dataclass(frozen=True)
class QueryEncoding:
    embeddings: ImageEmbeddings
    clip_ms: float
    csd_ms: float
    normalize_ms: float
    csd_preprocess_ms: float = 0.0
    csd_tensor_transfer_ms: float = 0.0
    csd_backbone_ms: float = 0.0
    csd_projection_ms: float = 0.0
    csd_output_ms: float = 0.0


class QueryEncoder(Protocol):
    def encode(self, image: Image.Image) -> QueryEncoding:
        """Return version-compatible normalized query vectors."""


class PinnedQueryEncoder:
    """Use the exact corpus CLIP and CSD models for uploaded query images."""

    def __init__(
        self,
        app_config: AppConfig,
        qdrant_config: QdrantConfig,
        *,
        provider: HuggingFaceEmbeddingProvider | None = None,
    ) -> None:
        self.qdrant_config = qdrant_config
        self.provider = provider or HuggingFaceEmbeddingProvider(app_config)

    def encode(self, image: Image.Image) -> QueryEncoding:
        values, timing = self.provider.embed_pil_images_timed([image])
        if len(values) != 1:
            raise RuntimeError("query encoder did not return exactly one embedding")
        normalize_started = time.perf_counter()
        normalized = normalize_embeddings(values[0])
        normalize_ms = (time.perf_counter() - normalize_started) * 1000.0
        if normalized.clip_dim != self.qdrant_config.clip_dimension:
            raise RuntimeError("query CLIP dimension does not match the active Qdrant collection")
        if normalized.csd_style_dim != self.qdrant_config.csd_dimension:
            raise RuntimeError("query CSD dimension does not match the active Qdrant collection")
        return QueryEncoding(
            embeddings=normalized,
            clip_ms=timing.clip_ms,
            csd_ms=timing.csd_ms,
            normalize_ms=normalize_ms,
            csd_preprocess_ms=timing.csd_preprocess_ms,
            csd_tensor_transfer_ms=timing.csd_tensor_transfer_ms,
            csd_backbone_ms=timing.csd_backbone_ms,
            csd_projection_ms=timing.csd_projection_ms,
            csd_output_ms=timing.csd_output_ms,
        )
