from pathlib import Path

from artsearch.deployment.preflight import run_deployment_preflight


def _write_config(path: Path) -> None:
    path.write_text(
        """
version = "1.2.0"

[object_store]
provider = "s3"
local_root = "data/object_store"
bucket = "artsearch-artifacts-alpha"
prefix = "artsearch"
endpoint_url = "https://account-id.r2.cloudflarestorage.com"
region = "auto"

[manifests]
local_dir = "data/production/manifests"
object_prefix = "manifests"

[postgres]
enabled = true
require_tls = true
database_url_env = "DATABASE_URL"
direct_url_env = "DATABASE_DIRECT_URL"
application_name = "artsearch-alpha"

[qdrant]
enabled = true
url = ""
url_env = "QDRANT_URL"
api_key_env = "QDRANT_API_KEY"
require_api_key = true
collection_name = "artworks_clip3_csd1_v1"
alias_name = "artworks_active"
clip_vector_name = "clip_subject"
csd_vector_name = "csd_style"
clip_dimension = 512
csd_dimension = 768
datatype = "float16"
on_disk_vectors = false
on_disk_payload = false
batch_size = 256
timeout_seconds = 30
prefer_grpc = false
hnsw_ef = 128
prefetch_limit = 200
fusion_limit = 100
max_results_per_artist = 2
require_sfw = true
require_bluesky_accept = true
promote_after_sync = true
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _canary_environment() -> dict[str, str]:
    return {
        "AWS_ACCESS_KEY_ID": "hidden-access",
        "AWS_SECRET_ACCESS_KEY": "hidden-secret",
        "QDRANT_URL": "https://cluster.cloud.qdrant.io:6333",
        "QDRANT_API_KEY": "hidden-qdrant",
    }


def test_canary_preflight_passes_with_cloud_contracts(tmp_path: Path) -> None:
    config = tmp_path / "production.alpha.local.toml"
    _write_config(config)

    report = run_deployment_preflight(config, environ=_canary_environment())

    assert report.ready is True
    assert report.to_dict()["summary"]["fail"] == 0
    assert any(check.check_id == "object_store.r2_region" for check in report.checks)


def test_api_phase_requires_database_and_session_secret(tmp_path: Path) -> None:
    config = tmp_path / "production.alpha.local.toml"
    _write_config(config)

    report = run_deployment_preflight(
        config,
        phase="api",
        environ=_canary_environment(),
    )

    assert report.ready is False
    failures = {check.check_id for check in report.checks if check.status == "fail"}
    assert failures == {"postgres.database_url", "api.session_secret"}


def test_report_never_serializes_secret_values(tmp_path: Path) -> None:
    config = tmp_path / "production.alpha.local.toml"
    _write_config(config)
    environment = {
        **_canary_environment(),
        "DATABASE_URL": "postgresql://sensitive",
        "DATABASE_DIRECT_URL": "postgresql://direct-sensitive",
        "ARTSEARCH_SESSION_SECRET": "s" * 32,
        "MODAL_TOKEN_ID": "sensitive-modal-id",
        "MODAL_TOKEN_SECRET": "sensitive-modal-secret",
    }

    serialized = str(run_deployment_preflight(config, phase="full", environ=environment).to_dict())

    for value in environment.values():
        assert value not in serialized
