from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
import sys
import tomllib

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts/configure_r2_credentials.py"
SPEC = importlib.util.spec_from_file_location("configure_r2_credentials", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
configure_credentials = MODULE.configure_credentials
derive_secret_access_key = MODULE.derive_secret_access_key


TEMPLATE = """\
version = "test"

[object_store]
provider = "local"
bucket = ""
endpoint_url = ""
region = ""

[qdrant]
enabled = true
"""


def test_configure_credentials_updates_ignored_runtime_files(tmp_path: Path) -> None:
    env_path = tmp_path / "deploy/alpha/.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "QDRANT_API_KEY=preserved\nAWS_ACCESS_KEY_ID=\nAWS_SECRET_ACCESS_KEY=\n",
        encoding="utf-8",
    )
    template_path = tmp_path / "production.example.toml"
    template_path.write_text(TEMPLATE, encoding="utf-8")
    config_path = tmp_path / "production.local.toml"

    configure_credentials(
        env_path=env_path,
        config_path=config_path,
        template_path=template_path,
        bucket="artsearch-artifacts-alpha",
        endpoint="https://account.us.r2.cloudflarestorage.com/",
        access_key="access-key-123456",
        secret_key="secret-key-123456",
    )

    env_text = env_path.read_text(encoding="utf-8")
    assert "QDRANT_API_KEY=preserved" in env_text
    assert "AWS_ACCESS_KEY_ID=access-key-123456" in env_text
    assert "AWS_SECRET_ACCESS_KEY=secret-key-123456" in env_text
    assert "AWS_SESSION_TOKEN=" in env_text
    assert "ARTSEARCH_R2_BUCKET=artsearch-artifacts-alpha" in env_text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["object_store"] == {
        "provider": "s3",
        "bucket": "artsearch-artifacts-alpha",
        "endpoint_url": "https://account.us.r2.cloudflarestorage.com",
        "region": "auto",
    }
    assert config["qdrant"]["enabled"] is True
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_token_value_is_derived_without_being_persisted() -> None:
    token_value = "one-time-cloudflare-token-value"

    derived = derive_secret_access_key(token_value)

    assert derived == "40902eb7991299e88d00869cef7e869c7dcca445c4687fa7acaf371e6bd1e210"
    assert token_value not in derived


def test_existing_secret_access_key_is_preserved() -> None:
    secret = "a" * 64

    assert derive_secret_access_key(secret) == secret


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://account.r2.cloudflarestorage.com",
        "https://example.com",
        "https://user@account.r2.cloudflarestorage.com",
        "https://account.r2.cloudflarestorage.com/path",
    ),
)
def test_configure_credentials_rejects_unsafe_endpoint(
    tmp_path: Path,
    endpoint: str,
) -> None:
    template_path = tmp_path / "production.example.toml"
    template_path.write_text(TEMPLATE, encoding="utf-8")

    with pytest.raises(ValueError, match="jurisdiction endpoint"):
        configure_credentials(
            env_path=tmp_path / ".env",
            config_path=tmp_path / "production.local.toml",
            template_path=template_path,
            bucket="artsearch-artifacts-alpha",
            endpoint=endpoint,
            access_key="access-key-123456",
            secret_key="secret-key-123456",
        )

    assert not (tmp_path / ".env").exists()
