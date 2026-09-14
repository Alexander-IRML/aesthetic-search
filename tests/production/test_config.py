from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.production.config import (
    ObjectStoreConfig,
    FaissConfig,
    PostgresConfig,
    QdrantConfig,
    ScaleBudgetConfig,
    load_production_config,
)
from artsearch.production.object_store import LocalObjectStore, build_object_store


def test_load_production_config_resolves_local_paths(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "production.toml"
    path.write_text(
        """
version = "test"
[object_store]
provider = "local"
local_root = "data/objects"
prefix = "artsearch"
[manifests]
local_dir = "data/manifests"
object_prefix = "manifests"
""",
        encoding="utf-8",
    )

    config = load_production_config(path)
    store = build_object_store(config.object_store)

    assert config.object_store.local_root == tmp_path / "data/objects"
    assert config.manifests.local_dir == tmp_path / "data/manifests"
    assert len(config.config_hash) == 64
    assert isinstance(store, LocalObjectStore)
    assert store.root == tmp_path / "data/objects/artsearch"

    second_root = tmp_path / "other"
    (second_root / "configs").mkdir(parents=True)
    second_path = second_root / "configs/production.toml"
    second_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    assert load_production_config(second_path).config_hash == config.config_hash


def test_qdrant_config_validates_serving_funnel() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        QdrantConfig(collection_name="same", alias_name="same")
    with pytest.raises(ValidationError, match="fusion_limit"):
        QdrantConfig(prefetch_limit=10, fusion_limit=11)
    with pytest.raises(ValidationError, match="distinct names"):
        QdrantConfig(clip_vector_name="same", csd_vector_name="same")


def test_faiss_config_validates_quantization_and_funnel() -> None:
    with pytest.raises(ValidationError, match="divisible"):
        FaissConfig(clip_dimension=513)
    with pytest.raises(ValidationError, match="nprobe"):
        FaissConfig(nlist=8, nprobe=9)
    with pytest.raises(ValidationError, match="limits"):
        FaissConfig(prefetch_limit=10, rerank_limit=21)


def test_scale_budget_must_account_for_exact_rerank_vectors() -> None:
    with pytest.raises(ValidationError, match="exact FP16"):
        ScaleBudgetConfig(store_exact_fp16_rerank_vectors=False)


def test_scale_example_pins_fifty_dollar_compact_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_production_config(root / "configs/production.scale-10m.example.toml")

    assert config.scale_budget.monthly_budget_usd == 50.0
    assert config.scale_budget.target_points == 10_000_000
    assert config.object_store.corpus_source == "processed"
    assert config.object_store.corpus_role == "display"
    assert config.object_store.publish_corpus_renditions is False
    assert config.local_retention.enabled is True
    assert config.local_retention.require_source_reference is True
    assert config.faiss.enabled is True
    assert config.qdrant.enabled is False
    assert config.postgres.publish_embeddings is False
    assert config.postgres.publish_filter_history is False
    assert config.postgres.publish_run_history is False


def test_scale_filter_profile_does_not_retain_image_features() -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_artwork_filter_config(
        root / "configs/artwork_filter.scale-10m.example.toml"
    )

    assert config.model.cache_embeddings is False
    assert config.storage.download_review_images is False


def test_s3_config_requires_bucket_and_https() -> None:
    with pytest.raises(ValidationError, match="bucket"):
        ObjectStoreConfig(provider="s3")
    with pytest.raises(ValidationError, match="HTTPS"):
        ObjectStoreConfig(
            provider="s3",
            bucket="bucket",
            endpoint_url="http://insecure.example",
        )
    with pytest.raises(ValidationError, match="upload_concurrency"):
        ObjectStoreConfig(upload_concurrency=0)


def test_alpha_cloud_config_uses_bounded_parallel_object_publication() -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_production_config(root / "configs/production.alpha-cloud.example.toml")

    assert config.object_store.upload_concurrency == 16
    assert config.object_store.upload_checkpoint_batch_size == 256
    assert config.object_store.upload_max_attempts == 5


def test_postgres_config_separates_runtime_and_migration_urls() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        PostgresConfig(database_url_env="DATABASE_URL", direct_url_env="DATABASE_URL")
    with pytest.raises(ValidationError, match="pool_size"):
        PostgresConfig(pool_size=0)
