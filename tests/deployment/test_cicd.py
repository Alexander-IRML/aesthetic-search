from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_ci_has_private_pull_request_checks_and_no_secrets() -> None:
    path = ROOT / ".github/workflows/ci.yml"
    source = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["pull_request"]["branches"] == ["main"]
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "pull_request_target" not in source
    assert "secrets." not in source
    assert set(workflow["jobs"]) == {"python", "postgres-migrations", "worker", "compose"}
    assert "pytest -q" in source
    assert "ruff check ." in source
    assert "--dry-run" in source
    assert "--containers-rollout=none" in source
    assert "pgvector/pgvector:0.8.6-pg17-bookworm" in source
    assert "git ls-files" in source


def test_alpha_deploy_is_verified_serialized_and_environment_gated() -> None:
    path = ROOT / ".github/workflows/deploy-alpha.yml"
    source = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    deploy = workflow["jobs"]["deploy"]

    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert workflow["concurrency"] == {
        "group": "deploy-alpha",
        "cancel-in-progress": False,
    }
    assert workflow["jobs"]["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert deploy["needs"] == "verify"
    assert deploy["environment"]["name"] == "alpha"
    assert source.index("Apply forward-compatible database migrations") < source.index(
        "Deploy Cloudflare Worker and Container"
    )
    assert source.index("Deploy Cloudflare Worker and Container") < source.index(
        "Smoke-test edge and static application"
    )
    for secret in (
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "DATABASE_DIRECT_URL",
    ):
        assert f"secrets.{secret}" in source
    assert "QDRANT_API_KEY" not in source
    assert "AWS_SECRET_ACCESS_KEY" not in source


def test_release_metadata_is_consistent() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/check_release_version.py", "--tag", "v0.7.0"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "agrees on 0.7.0" in completed.stdout


def test_release_metadata_rejects_wrong_tag() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/check_release_version.py", "--tag", "v9.9.9"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "does not match v0.7.0" in completed.stderr
