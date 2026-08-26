from __future__ import annotations

from pathlib import Path
import tomllib

import yaml

from artsearch.production.config import load_production_config


ROOT = Path(__file__).resolve().parents[2]


def test_project_declares_the_container_python_contract() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["requires-python"] == ">=3.12,<3.13"
    assert project["tool"]["ruff"]["target-version"] == "py312"


def test_application_dockerfile_has_control_ml_and_test_targets() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "python:3.12.14-slim-bookworm" in dockerfile
    assert "FROM runtime-base AS runtime" in dockerfile
    assert "FROM runtime-base AS ml" in dockerfile
    assert "FROM runtime-base AS test" in dockerfile
    assert "https://download.pytorch.org/whl/cpu" in dockerfile
    assert "pip check" in dockerfile
    assert "nvidia-" not in dockerfile.lower()
    assert "USER artsearch" in dockerfile


def test_local_compose_has_private_full_stack_contract() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {
        "postgres",
        "qdrant",
        "migrate",
        "dashboard",
        "api",
        "airflow",
        "ml",
    }
    assert services["postgres"]["image"] == "pgvector/pgvector:0.8.6-pg17-bookworm"
    assert services["qdrant"]["image"] == "qdrant/qdrant:v1.18.2"
    assert services["postgres"]["ports"] == ["127.0.0.1:5432:5432"]
    assert services["qdrant"]["ports"] == [
        "127.0.0.1:${ARTSEARCH_QDRANT_HTTP_PORT:-6335}:6333",
        "127.0.0.1:${ARTSEARCH_QDRANT_GRPC_PORT:-6336}:6334",
    ]
    assert services["dashboard"]["ports"] == ["127.0.0.1:${ARTSEARCH_DASHBOARD_PORT:-8765}:8765"]
    assert services["api"]["ports"] == ["127.0.0.1:${ARTSEARCH_API_PORT:-8000}:8000"]
    assert services["airflow"]["ports"] == ["127.0.0.1:${AIRFLOW_PORT:-8081}:8080"]
    assert services["dashboard"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert "--allow-remote-review" in services["dashboard"]["command"]
    assert services["api"]["profiles"] == ["search"]
    assert services["ml"]["profiles"] == ["tools"]
    expected_user = "${ARTSEARCH_UID:-1000}:${ARTSEARCH_GID:-1000}"
    for service_name in ("migrate", "dashboard", "api", "ml"):
        assert services[service_name]["user"] == expected_user


def test_docker_production_config_uses_internal_service_names() -> None:
    config = load_production_config(ROOT / "configs/production.docker.toml")

    assert config.postgres.enabled is True
    assert config.postgres.require_tls is False
    assert config.postgres.application_name == "artsearch-docker"
    assert config.qdrant.enabled is True
    assert config.qdrant.url == "http://qdrant:6333"


def test_runtime_constraints_cover_every_heavy_boundary() -> None:
    constraints = (ROOT / "constraints/runtime-py312.txt").read_text(encoding="utf-8")

    for requirement in (
        "torch==2.13.0",
        "torchvision==0.28.0",
        "transformers==5.13.0",
        "pandas==2.3.3",
        "pyspark",
        "qdrant-client==1.18.0",
        "fastapi==0.141.1",
        "uvicorn==0.51.0",
        "psycopg==3.3.4",
        "sqlalchemy==2.0.52",
    ):
        if requirement == "pyspark":
            airflow = (ROOT / "orchestration/airflow/requirements.txt").read_text(encoding="utf-8")
            assert "pyspark==4.2.0" in airflow
        else:
            assert requirement in constraints
