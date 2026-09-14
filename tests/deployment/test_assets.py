from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_alpha_control_plane_keeps_operator_surface_private() -> None:
    compose = yaml.safe_load((ROOT / "deploy/alpha/compose.yaml").read_text(encoding="utf-8"))

    services = compose["services"]
    assert set(services) == {"airflow"}
    assert services["airflow"]["ports"] == ["127.0.0.1:${AIRFLOW_PORT:-8080}:8080"]
    assert services["airflow"]["environment"]["QDRANT_URL"] == "${QDRANT_URL:-}"
    assert services["airflow"]["environment"]["AWS_SECRET_ACCESS_KEY"] == (
        "${AWS_SECRET_ACCESS_KEY:-}"
    )


def test_alpha_environment_template_contains_no_committed_credentials() -> None:
    template = (ROOT / "deploy/alpha/.env.example").read_text(encoding="utf-8")

    for variable in (
        "AWS_SECRET_ACCESS_KEY",
        "QDRANT_API_KEY",
        "DATABASE_URL",
        "ARTSEARCH_SESSION_SECRET",
        "ARTSEARCH_VISITOR_HASH_SECRET",
        "MODAL_TOKEN_SECRET",
    ):
        assert f"{variable}=\n" in template
