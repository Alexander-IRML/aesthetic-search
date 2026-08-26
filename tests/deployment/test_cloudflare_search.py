from __future__ import annotations

import json
from pathlib import Path

from artsearch.api.config import load_search_api_config
from artsearch.production.config import load_production_config


ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = ROOT / "deploy/cloudflare-search"


def test_cloudflare_container_contract_is_small_and_single_owner() -> None:
    config = json.loads((DEPLOYMENT / "wrangler.jsonc").read_text(encoding="utf-8"))

    assert config["compatibility_date"] == "2026-08-21"
    assert config["compatibility_flags"] == ["nodejs_compat"]
    assert config["observability"]["enabled"] is True
    assert config["r2_buckets"] == [
        {
            "binding": "ARTSEARCH_MEDIA",
            "bucket_name": "artsearch-artifacts-alpha",
        }
    ]
    container = config["containers"][0]
    assert container["max_instances"] == 1
    assert container["instance_type"] == {
        "vcpu": 1,
        "memory_mib": 4096,
        "disk_mb": 8000,
    }
    assert container["constraints"]["regions"] == ["WNAM"]
    assert config["migrations"][0]["new_sqlite_classes"] == ["ArtSearchApiContainer"]


def test_cloudflare_runtime_configs_use_external_catalog_and_named_vectors() -> None:
    production = load_production_config(ROOT / "configs/production.cloudflare.toml")
    api = load_search_api_config(ROOT / "configs/search_api.cloudflare.toml")

    assert production.postgres.enabled is True
    assert production.postgres.require_tls is True
    assert production.postgres.pool_size == 2
    assert production.qdrant.require_api_key is True
    assert production.qdrant.datatype == "float16"
    assert production.qdrant.batch_size == 256
    assert api.catalog_backend == "postgres"
    assert api.serve_local_images is False
    assert api.asset_base_url == "/media/artsearch"
    assert api.max_concurrent_inferences == 1


def test_cloudflare_image_bakes_models_and_starts_offline() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM ml AS cloudflare-search" in dockerfile
    assert "python -m artsearch.api.model_cache" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "TRANSFORMERS_OFFLINE=1" in dockerfile
    assert "configs/production.cloudflare.toml" in dockerfile
    assert "configs/search_api.cloudflare.toml" in dockerfile


def test_cloudflare_worker_does_not_contain_secret_values() -> None:
    source = (DEPLOYMENT / "src/index.ts").read_text(encoding="utf-8")

    assert "QDRANT_API_KEY: env.QDRANT_API_KEY" in source
    assert "DATABASE_URL: env.DATABASE_URL" in source
    assert 'jpg: "image/jpeg"' in source
    assert 'webp: "image/webp"' in source
    assert "eyJ" not in source
    assert "postgresql://" not in source
