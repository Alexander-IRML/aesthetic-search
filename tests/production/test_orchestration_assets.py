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
    compose = yaml.safe_load(
        (ROOT / "orchestration/airflow/compose.yaml").read_text(encoding="utf-8")
    )
    service = compose["services"]["airflow"]

    assert "apache/airflow:3.3.0-python3.12" in dockerfile
    assert "openjdk-17-jre-headless" in dockerfile
    assert '"pyspark==4.2.0"' in dockerfile
    assert service["environment"]["AIRFLOW__CORE__LOAD_EXAMPLES"] == "false"
    assert "../../data:/opt/artsearch/data" in service["volumes"]


def test_docker_context_excludes_private_corpus_and_local_secrets() -> None:
    patterns = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())

    assert "data" in patterns
    assert ".env" in patterns
    assert "config/bluesky_*.local.txt" in patterns
    assert "configs/*.local.toml" in patterns
