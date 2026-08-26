from __future__ import annotations

from typing import Any

from artsearch.ingest.config import ModelConfig
from artsearch.production.config import QdrantConfig
from artsearch.retrieval.qdrant import model_bundle_version


def model_bundle_record(models: ModelConfig, qdrant: QdrantConfig) -> dict[str, Any]:
    """Build one canonical lineage row shared by PostgreSQL and Qdrant."""

    return {
        "model_bundle_version": model_bundle_version(models, qdrant),
        "clip_model_name": models.clip_model_name,
        "clip_model_revision": models.clip_model_version,
        "csd_model_name": models.csd_model_name,
        "csd_model_revision": models.csd_model_version,
        "clip_dimension": qdrant.clip_dimension,
        "csd_dimension": qdrant.csd_dimension,
        "status": "active",
        "metadata": {
            "qdrant_collection": qdrant.collection_name,
            "clip_vector_name": qdrant.clip_vector_name,
            "csd_vector_name": qdrant.csd_vector_name,
            "qdrant_datatype": qdrant.datatype,
        },
    }
