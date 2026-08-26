from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlparse

from artsearch.production.config import load_production_config


DeploymentPhase = Literal["canary", "api", "full"]
CheckStatus = Literal["pass", "warn", "fail"]

_PLACEHOLDER_MARKERS = ("YOUR_", "REPLACE", "EXAMPLE", "<", ">")


@dataclass(frozen=True)
class DeploymentCheck:
    check_id: str
    status: CheckStatus
    message: str
    required_for: tuple[DeploymentPhase, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DeploymentReport:
    phase: DeploymentPhase
    production_config: str
    checks: tuple[DeploymentCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        counts = {
            status: sum(check.status == status for check in self.checks)
            for status in ("pass", "warn", "fail")
        }
        return {
            "phase": self.phase,
            "production_config": self.production_config,
            "ready": self.ready,
            "summary": counts,
            "checks": [check.to_dict() for check in self.checks],
        }


def run_deployment_preflight(
    production_config: str | Path,
    *,
    phase: DeploymentPhase = "canary",
    environ: Mapping[str, str] | None = None,
) -> DeploymentReport:
    """Validate non-secret contracts without contacting a cloud provider."""

    if phase not in {"canary", "api", "full"}:
        raise ValueError(f"unsupported deployment phase: {phase}")
    environment = os.environ if environ is None else environ
    path = Path(production_config)
    config = load_production_config(path)
    checks: list[DeploymentCheck] = []

    checks.append(
        _boolean_check(
            "object_store.provider",
            config.object_store.provider == "s3",
            success="S3-compatible object storage is selected.",
            failure="Cloud canaries require object_store.provider='s3'.",
            required_for=("canary", "api", "full"),
            phase=phase,
        )
    )
    checks.append(
        _value_check(
            "object_store.bucket",
            config.object_store.bucket,
            success="Object-store bucket is configured.",
            missing="Object-store bucket is missing or still a placeholder.",
            required_for=("canary", "api", "full"),
            phase=phase,
        )
    )
    endpoint = config.object_store.endpoint_url.strip()
    endpoint_valid = _https_url(endpoint) and not _placeholder(endpoint)
    checks.append(
        _boolean_check(
            "object_store.endpoint",
            endpoint_valid,
            success="Object-store HTTPS endpoint is configured.",
            failure="Set a real HTTPS R2 endpoint in the ignored production config.",
            required_for=("canary", "api", "full"),
            phase=phase,
        )
    )
    if endpoint_valid and endpoint.endswith("r2.cloudflarestorage.com"):
        checks.append(
            _boolean_check(
                "object_store.r2_region",
                config.object_store.region == "auto",
                success="R2 region is set to 'auto'.",
                failure="Cloudflare R2 should use object_store.region='auto'.",
                required_for=("canary", "api", "full"),
                phase=phase,
            )
        )

    checks.extend(
        _secret_checks(
            environment,
            (
                ("aws.access_key", "AWS_ACCESS_KEY_ID", ("canary", "api", "full")),
                ("aws.secret_key", "AWS_SECRET_ACCESS_KEY", ("canary", "api", "full")),
            ),
            phase=phase,
        )
    )

    qdrant = config.qdrant
    checks.append(
        _boolean_check(
            "qdrant.enabled",
            qdrant.enabled,
            success="Qdrant publication is enabled.",
            failure="Cloud canaries require qdrant.enabled=true.",
            required_for=("canary", "api", "full"),
            phase=phase,
        )
    )
    qdrant_url = environment.get(qdrant.url_env, "").strip() or qdrant.url.strip()
    checks.append(
        _boolean_check(
            "qdrant.endpoint",
            _https_url(qdrant_url) and not _placeholder(qdrant_url),
            success=f"Qdrant HTTPS endpoint is configured through {qdrant.url_env}.",
            failure=f"Set a real HTTPS Qdrant endpoint through {qdrant.url_env}.",
            required_for=("canary", "api", "full"),
            phase=phase,
        )
    )
    checks.append(
        _boolean_check(
            "qdrant.require_api_key",
            qdrant.require_api_key,
            success="Qdrant API-key enforcement is enabled.",
            failure="Set qdrant.require_api_key=true for cloud deployments.",
            required_for=("canary", "api", "full"),
            phase=phase,
        )
    )
    checks.extend(
        _secret_checks(
            environment,
            (("qdrant.api_key", qdrant.api_key_env, ("canary", "api", "full")),),
            phase=phase,
        )
    )

    postgres = config.postgres
    checks.append(
        _boolean_check(
            "postgres.enabled",
            postgres.enabled,
            success="Canonical PostgreSQL publication is enabled.",
            failure="Set postgres.enabled=true before API or full pipeline deployment.",
            required_for=("api", "full"),
            phase=phase,
        )
    )
    checks.append(
        _boolean_check(
            "postgres.require_tls",
            postgres.require_tls,
            success="PostgreSQL TLS enforcement is enabled.",
            failure="Set postgres.require_tls=true for cloud connections.",
            required_for=("api", "full"),
            phase=phase,
        )
    )
    checks.extend(
        _secret_checks(
            environment,
            (
                (
                    "postgres.database_url",
                    postgres.database_url_env,
                    ("api", "full"),
                ),
                (
                    "postgres.direct_url",
                    postgres.direct_url_env,
                    ("full",),
                ),
                ("modal.token_id", "MODAL_TOKEN_ID", ("full",)),
                ("modal.token_secret", "MODAL_TOKEN_SECRET", ("full",)),
            ),
            phase=phase,
        )
    )
    session_secret = environment.get("ARTSEARCH_SESSION_SECRET", "")
    checks.append(
        _boolean_check(
            "api.session_secret",
            len(session_secret.encode("utf-8")) >= 32,
            success="API session secret is at least 32 bytes.",
            failure="Set ARTSEARCH_SESSION_SECRET to a generated value of at least 32 bytes.",
            required_for=("api", "full"),
            phase=phase,
        )
    )

    suffix_status: CheckStatus = "pass" if path.name.endswith(".local.toml") else "warn"
    checks.append(
        DeploymentCheck(
            check_id="config.local_copy",
            status=suffix_status,
            message=(
                "Using an ignored local production config."
                if suffix_status == "pass"
                else "Use an ignored *.local.toml copy before adding real account identifiers."
            ),
            required_for=("canary", "api", "full"),
        )
    )

    return DeploymentReport(
        phase=phase,
        production_config=str(path),
        checks=tuple(checks),
    )


def _secret_checks(
    environment: Mapping[str, str],
    specifications: tuple[tuple[str, str, tuple[DeploymentPhase, ...]], ...],
    *,
    phase: DeploymentPhase,
) -> list[DeploymentCheck]:
    return [
        _boolean_check(
            check_id,
            bool(environment.get(variable, "").strip()),
            success=f"{variable} is present (value hidden).",
            failure=f"Set {variable} in the runtime secret environment.",
            required_for=required_for,
            phase=phase,
        )
        for check_id, variable, required_for in specifications
    ]


def _value_check(
    check_id: str,
    value: str,
    *,
    success: str,
    missing: str,
    required_for: tuple[DeploymentPhase, ...],
    phase: DeploymentPhase,
) -> DeploymentCheck:
    return _boolean_check(
        check_id,
        bool(value.strip()) and not _placeholder(value),
        success=success,
        failure=missing,
        required_for=required_for,
        phase=phase,
    )


def _boolean_check(
    check_id: str,
    condition: bool,
    *,
    success: str,
    failure: str,
    required_for: tuple[DeploymentPhase, ...],
    phase: DeploymentPhase,
) -> DeploymentCheck:
    if condition:
        status: CheckStatus = "pass"
        message = success
    elif phase in required_for:
        status = "fail"
        message = failure
    else:
        status = "warn"
        message = f"Later phase: {failure}"
    return DeploymentCheck(
        check_id=check_id,
        status=status,
        message=message,
        required_for=required_for,
    )


def _placeholder(value: str) -> bool:
    upper = value.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def _https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.username is None
