from __future__ import annotations

from typing import Protocol

from PIL import Image

from artsearch.embed.models import HuggingFaceEmbeddingProvider
from artsearch.embed.storage import ImageEmbeddings, normalize_embeddings
from artsearch.ingest.config import AppConfig
from artsearch.production.config import QdrantConfig


class QueryEncoder(Protocol):
    def encode(self, image: Image.Image) -> ImageEmbeddings:
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

    def encode(self, image: Image.Image) -> ImageEmbeddings:
        values = self.provider.embed_pil_images([image])
        if len(values) != 1:
            raise RuntimeError("query encoder did not return exactly one embedding")
        normalized = normalize_embeddings(values[0])
        if normalized.clip_dim != self.qdrant_config.clip_dimension:
            raise RuntimeError("query CLIP dimension does not match the active Qdrant collection")
        if normalized.csd_style_dim != self.qdrant_config.csd_dimension:
            raise RuntimeError("query CSD dimension does not match the active Qdrant collection")
        return normalized
