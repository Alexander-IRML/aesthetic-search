from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "relative_path",
    [
        "orchestration/airflow/dags/artsearch_intake.py",
        "orchestration/airflow/dags/artsearch_corpus_audit.py",
        "jobs/spark/corpus_audit.py",
    ],
)
def test_orchestration_python_assets_parse_without_optional_runtimes(
    relative_path: str,
) -> None:
    path = ROOT / relative_path
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_airflow_container_has_isolated_supported_runtime() -> None:
    dockerfile = (ROOT / "orchestration/airflow/Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "orchestration/airflow/requirements.txt").read_text(encoding="utf-8")
    compose = yaml.safe_load(
        (ROOT / "orchestration/airflow/compose.yaml").read_text(encoding="utf-8")
    )
    service = compose["services"]["airflow"]

    assert "apache/airflow:${AIRFLOW_VERSION}-python3.12" in dockerfile
    assert "AIRFLOW_VERSION=3.3.1" in dockerfile
    assert "openjdk-17-jre-headless" in dockerfile
    assert "apache-airflow-providers-apache-spark[pyspark]==6.3.1" in requirements
    assert "pyspark==4.2.0" in requirements
    assert '".[data,embed,filter,postgres,qdrant]"' in dockerfile
    assert '"pandas==2.3.3"' in dockerfile
    assert 'constraint "${HOME}/constraints.txt"' in dockerfile
    assert "pip check" in dockerfile
    assert service["environment"]["AIRFLOW__CORE__LOAD_EXAMPLES"] == "false"
    assert service["environment"]["QDRANT_URL"] == "${QDRANT_URL:-http://qdrant:6333}"
    assert service["environment"]["DATABASE_URL"] == "${DATABASE_URL:-}"
    assert service["environment"]["DATABASE_DIRECT_URL"] == "${DATABASE_DIRECT_URL:-}"
    assert service["environment"]["ARTSEARCH_PRODUCTION_CONFIG"] == (
        "${ARTSEARCH_PRODUCTION_CONFIG:-configs/production.default.toml}"
    )
    assert service["environment"]["ARTSEARCH_ACTORS_FILE"] == (
        "${ARTSEARCH_ACTORS_FILE:-config/bluesky_actors.local.txt}"
    )
    assert "../../data:/opt/artsearch/data" in service["volumes"]
    assert compose["services"]["qdrant"]["image"] == "qdrant/qdrant:v1.18.2"
    assert "../../data/qdrant:/qdrant/storage" in compose["services"]["qdrant"]["volumes"]


def test_docker_context_excludes_private_corpus_and_local_secrets() -> None:
    patterns = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())

    assert "data" in patterns
    assert ".env" in patterns
    assert "config/bluesky_*.local.txt" in patterns
    assert "configs/*.local.toml" in patterns
