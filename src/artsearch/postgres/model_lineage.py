from __future__ import annotations

from typing import Any

from artsearch.ingest.config import ModelConfig
from artsearch.production.config import FaissConfig, ProductionConfig, QdrantConfig
from artsearch.retrieval.faiss_index import faiss_model_bundle_version
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


def faiss_model_bundle_record(models: ModelConfig, faiss: FaissConfig) -> dict[str, Any]:
    """Build lineage for the compact FAISS serving representation."""

    return {
        "model_bundle_version": faiss_model_bundle_version(models, faiss),
        "clip_model_name": models.clip_model_name,
        "clip_model_revision": models.clip_model_version,
        "csd_model_name": models.csd_model_name,
        "csd_model_revision": models.csd_model_version,
        "clip_dimension": faiss.clip_dimension,
        "csd_dimension": faiss.csd_dimension,
        "status": "active",
        "metadata": {
            "vector_backend": "faiss",
            "faiss_alias": faiss.alias_name,
            "index_kind": "opq_ivf_pq",
            "rerank_dtype": "float16",
        },
    }


def configured_model_bundle_record(
    models: ModelConfig,
    production: ProductionConfig,
) -> dict[str, Any]:
    """Describe the configured serving backend, preferring the live Qdrant path."""

    if production.qdrant.enabled:
        return model_bundle_record(models, production.qdrant)
    if production.faiss.enabled:
        return faiss_model_bundle_record(models, production.faiss)
    return model_bundle_record(models, production.qdrant)
