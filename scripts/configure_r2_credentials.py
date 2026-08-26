#!/usr/bin/env python3
"""Securely configure local Cloudflare R2 publication credentials."""

from __future__ import annotations

import getpass
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import tomllib
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = PROJECT_ROOT / "deploy/alpha/.env"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs/production.alpha-cloud.local.toml"
CONFIG_TEMPLATE_PATH = PROJECT_ROOT / "configs/production.alpha-cloud.example.toml"
DEFAULT_BUCKET = "artsearch-artifacts-alpha"

_SAFE_CREDENTIAL = re.compile(r"^[A-Za-z0-9_./+=:-]+$")


def main() -> None:
    print("Configure Cloudflare R2 for the ArtSearch corpus publisher.")
    print(f"Bucket: {DEFAULT_BUCKET}")
    print("Input is written only to ignored local files; secrets are never echoed.\n")

    endpoint = input("R2 jurisdiction endpoint: ").strip()
    access_key = getpass.getpass("R2 Access Key ID (hidden): ").strip()
    secret_material = getpass.getpass("R2 Secret Access Key or API Token Value (hidden): ").strip()
    secret_key = derive_secret_access_key(secret_material)

    configure_credentials(
        env_path=DEFAULT_ENV_PATH,
        config_path=DEFAULT_CONFIG_PATH,
        template_path=CONFIG_TEMPLATE_PATH,
        bucket=DEFAULT_BUCKET,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
    )
    print("\nR2 credentials configured successfully.")
    print(f"  secrets: {DEFAULT_ENV_PATH.relative_to(PROJECT_ROOT)} (mode 0600)")
    print(f"  config:  {DEFAULT_CONFIG_PATH.relative_to(PROJECT_ROOT)}")
    print("No credential values were printed.")


def derive_secret_access_key(value: str) -> str:
    """Convert Cloudflare's one-time token value without retaining the parent token."""

    normalized = _validate_credential("secret material", value)
    if re.fullmatch(r"[0-9a-f]{64}", normalized):
        return normalized
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def configure_credentials(
    *,
    env_path: Path,
    config_path: Path,
    template_path: Path,
    bucket: str,
    endpoint: str,
    access_key: str,
    secret_key: str,
) -> None:
    normalized_bucket = _validate_bucket(bucket)
    normalized_endpoint = _validate_endpoint(endpoint)
    normalized_access_key = _validate_credential("Access Key ID", access_key)
    normalized_secret_key = _validate_credential("Secret Access Key", secret_key)

    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env_text = _upsert_env_values(
        env_text,
        {
            "ARTSEARCH_R2_ENDPOINT": normalized_endpoint,
            "ARTSEARCH_R2_BUCKET": normalized_bucket,
            "AWS_ACCESS_KEY_ID": normalized_access_key,
            "AWS_SECRET_ACCESS_KEY": normalized_secret_key,
            "AWS_SESSION_TOKEN": "",
            "AWS_DEFAULT_REGION": "auto",
        },
    )
    _atomic_write(env_path, env_text, mode=0o600)

    source_path = config_path if config_path.exists() else template_path
    config_text = source_path.read_text(encoding="utf-8")
    config_text = _update_object_store_config(
        config_text,
        bucket=normalized_bucket,
        endpoint=normalized_endpoint,
    )
    parsed = tomllib.loads(config_text)
    object_store = parsed.get("object_store", {})
    if (
        object_store.get("provider") != "s3"
        or object_store.get("bucket") != normalized_bucket
        or object_store.get("endpoint_url") != normalized_endpoint
        or object_store.get("region") != "auto"
    ):
        raise RuntimeError("generated R2 configuration failed validation")
    _atomic_write(config_path, config_text, mode=0o600)


def _validate_bucket(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", normalized):
        raise ValueError("R2 bucket must be a valid lowercase bucket name")
    return normalized


def _validate_endpoint(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not host.endswith(".r2.cloudflarestorage.com")
    ):
        raise ValueError("use the HTTPS jurisdiction endpoint shown by Cloudflare R2")
    return normalized


def _validate_credential(label: str, value: str) -> str:
    normalized = value.strip()
    if len(normalized) < 16 or not _SAFE_CREDENTIAL.fullmatch(normalized):
        raise ValueError(f"{label} is empty or contains unsupported characters")
    return normalized


def _upsert_env_values(text: str, values: dict[str, str]) -> str:
    lines = text.splitlines()
    remaining = dict(values)
    updated: list[str] = []
    for line in lines:
        name = line.partition("=")[0].strip()
        if name in remaining and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            updated.append(f"{name}={remaining.pop(name)}")
        else:
            updated.append(line)
    if remaining:
        if updated and updated[-1]:
            updated.append("")
        updated.extend(f"{name}={value}" for name, value in remaining.items())
    return "\n".join(updated).rstrip() + "\n"


def _update_object_store_config(text: str, *, bucket: str, endpoint: str) -> str:
    replacements = {
        "provider": 'provider = "s3"',
        "bucket": f'bucket = "{bucket}"',
        "endpoint_url": f'endpoint_url = "{endpoint}"',
        "region": 'region = "auto"',
    }
    seen: set[str] = set()
    in_object_store = False
    updated: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_object_store:
                for key, replacement in replacements.items():
                    if key not in seen:
                        updated.append(replacement)
            in_object_store = stripped == "[object_store]"
        if in_object_store and "=" in stripped and not stripped.startswith("#"):
            key = stripped.partition("=")[0].strip()
            if key in replacements:
                updated.append(replacements[key])
                seen.add(key)
                continue
        updated.append(line)
    if in_object_store:
        for key, replacement in replacements.items():
            if key not in seen:
                updated.append(replacement)
    if not seen:
        raise ValueError("production config has no [object_store] section")
    return "\n".join(updated).rstrip() + "\n"


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
