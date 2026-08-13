from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from artsearch.production.config import (
    ObjectStoreConfig,
    QdrantConfig,
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
    with pytest.raises(ValidationError, match="patch_rerank_limit"):
        QdrantConfig(prefetch_limit=10, fusion_limit=10, patch_rerank_limit=11)


def test_s3_config_requires_bucket_and_https() -> None:
    with pytest.raises(ValidationError, match="bucket"):
        ObjectStoreConfig(provider="s3")
    with pytest.raises(ValidationError, match="HTTPS"):
        ObjectStoreConfig(
            provider="s3",
            bucket="bucket",
            endpoint_url="http://insecure.example",
        )
