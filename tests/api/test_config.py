from pathlib import Path

import pytest

from artsearch.api.config import SearchApiConfig, load_search_api_config


ROOT = Path(__file__).resolve().parents[2]


def test_default_search_api_contract_is_fixed_and_private() -> None:
    config = load_search_api_config(ROOT / "configs/search_api.default.toml")

    assert config.result_limit == 10
    assert config.candidate_telemetry_limit == 100
    assert config.feedback_rate_limit_requests == 120
    assert config.catalog_backend == "sqlite"
    assert config.serve_local_images is True
    assert config.trust_proxy_headers is False
    assert len(config.config_hash) == 64


def test_ranked_candidate_depth_must_cover_displayed_results() -> None:
    with pytest.raises(ValueError, match="cover every displayed result"):
        SearchApiConfig(result_limit=10, candidate_telemetry_limit=9)


def test_postgres_catalog_requires_routable_assets_and_no_local_image_route() -> None:
    with pytest.raises(ValueError, match="serve_local_images"):
        SearchApiConfig(catalog_backend="postgres")

    with pytest.raises(ValueError, match="asset_base_url"):
        SearchApiConfig(catalog_backend="postgres", serve_local_images=False)

    config = SearchApiConfig(
        catalog_backend="postgres",
        serve_local_images=False,
        asset_base_url="https://images.example.com/base/",
    )
    assert config.asset_base_url == "https://images.example.com/base"

    same_origin = SearchApiConfig(
        catalog_backend="postgres",
        serve_local_images=False,
        asset_base_url="/media/artsearch/",
    )
    assert same_origin.asset_base_url == "/media/artsearch"


def test_asset_base_url_can_be_injected_without_editing_versioned_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "api.toml"
    path.write_text(
        "\n".join(
            (
                'catalog_backend = "postgres"',
                "serve_local_images = false",
                'asset_base_url = ""',
                'asset_base_url_env = "TEST_ASSET_BASE_URL"',
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_ASSET_BASE_URL", "https://cdn.example.com/art/")

    config = load_search_api_config(path)

    assert config.asset_base_url == "https://cdn.example.com/art"
    assert len(config.config_hash) == 64
