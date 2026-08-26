from __future__ import annotations

import os
from pathlib import Path
import secrets
from urllib.parse import SplitResult, urlparse, urlsplit, urlunsplit
import uuid


def write_qdrant_credentials(
    destination: str | Path,
    *,
    endpoint: str,
    api_key: str,
    template: str | Path = "deploy/alpha/.env.example",
) -> Path:
    """Atomically update an ignored env file without exposing credential values."""

    normalized_endpoint = endpoint.strip().rstrip("/")
    parsed = urlparse(normalized_endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None:
        raise ValueError("Qdrant endpoint must be an HTTPS cluster URL")
    if not api_key.strip():
        raise ValueError("Qdrant database API key must not be empty")
    if any(character in api_key for character in ("\n", "\r", "\x00")):
        raise ValueError("Qdrant database API key contains an invalid character")

    return _write_env_values(
        destination,
        {
            "QDRANT_URL": normalized_endpoint,
            "QDRANT_API_KEY": api_key,
        },
        template=template,
    )


def write_postgres_credentials(
    destination: str | Path,
    *,
    pooled_url: str,
    direct_url: str,
    template: str | Path = "deploy/alpha/.env.example",
) -> Path:
    """Store separate pooled/runtime and direct/migration URLs privately."""

    pooled = _postgres_url(pooled_url, name="pooled")
    direct = _postgres_url(direct_url, name="direct")
    if pooled == direct:
        raise ValueError("pooled and direct PostgreSQL URLs must differ")
    return _write_env_values(
        destination,
        {"DATABASE_URL": pooled, "DATABASE_DIRECT_URL": direct},
        template=template,
    )


def write_neon_connection_string(
    destination: str | Path,
    *,
    connection_url: str,
    template: str | Path = "deploy/alpha/.env.example",
) -> Path:
    """Store one Neon URL as the derived pooled/runtime and direct URLs."""

    pooled, direct = derive_neon_connection_urls(connection_url)
    return write_postgres_credentials(
        destination,
        pooled_url=pooled,
        direct_url=direct,
        template=template,
    )


def ensure_session_secret(
    destination: str | Path,
    *,
    template: str | Path = "deploy/alpha/.env.example",
    minimum_bytes: int = 32,
) -> tuple[Path, bool]:
    """Create a private session secret unless a sufficiently strong one exists."""

    target = Path(destination)
    template_path = Path(template)
    source_text = (
        target.read_text(encoding="utf-8")
        if target.exists()
        else template_path.read_text(encoding="utf-8")
    )
    existing = _env_value(source_text, "ARTSEARCH_SESSION_SECRET")
    if len(existing.encode("utf-8")) >= minimum_bytes:
        target.chmod(0o600)
        return target, False
    if existing:
        raise ValueError(f"existing ARTSEARCH_SESSION_SECRET is shorter than {minimum_bytes} bytes")

    generated = secrets.token_urlsafe(max(48, minimum_bytes))
    written = _write_env_values(
        target,
        {"ARTSEARCH_SESSION_SECRET": generated},
        template=template_path,
    )
    return written, True


def derive_neon_connection_urls(connection_url: str) -> tuple[str, str]:
    """Return Neon pooled and direct variants without changing credentials."""

    normalized = _postgres_url(connection_url, name="Neon")
    parsed = urlsplit(normalized)
    hostname = parsed.hostname or ""
    if not hostname.endswith(".neon.tech"):
        raise ValueError("Neon connection URL hostname must end with .neon.tech")

    endpoint, separator, remainder = hostname.partition(".")
    if not separator or not endpoint.startswith("ep-"):
        raise ValueError("Neon connection URL must contain an ep-* endpoint hostname")

    if endpoint.endswith("-pooler"):
        pooled_hostname = hostname
        direct_hostname = f"{endpoint.removesuffix('-pooler')}.{remainder}"
    else:
        direct_hostname = hostname
        pooled_hostname = f"{endpoint}-pooler.{remainder}"

    return (
        _replace_url_hostname(parsed, pooled_hostname),
        _replace_url_hostname(parsed, direct_hostname),
    )


def _replace_url_hostname(parsed: SplitResult, hostname: str) -> str:
    userinfo, marker, server = parsed.netloc.rpartition("@")
    host, port_marker, port = server.partition(":")
    authority = f"{hostname}{port_marker}{port}"
    if marker:
        authority = f"{userinfo}@{authority}"
    return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, parsed.fragment))


def _postgres_url(value: str, *, name: str) -> str:
    normalized = value.strip()
    if any(character in normalized for character in ("\n", "\r", "\x00")):
        raise ValueError(f"{name} PostgreSQL URL contains an invalid character")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError(f"{name} PostgreSQL URL must use postgres:// or postgresql://")
    if not parsed.username or not parsed.password or not parsed.path.strip("/"):
        raise ValueError(f"{name} PostgreSQL URL must include user, password, and database")
    return normalized


def _env_value(source_text: str, name: str) -> str:
    prefix = f"{name}="
    for line in source_text.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    return ""


def _write_env_values(
    destination: str | Path,
    replacements: dict[str, str],
    *,
    template: str | Path,
) -> Path:
    target = Path(destination)
    template_path = Path(template)
    source_text = (
        target.read_text(encoding="utf-8")
        if target.exists()
        else template_path.read_text(encoding="utf-8")
    )
    output: list[str] = []
    found: set[str] = set()
    for line in source_text.splitlines():
        name = line.split("=", 1)[0].strip() if "=" in line else ""
        if name in replacements:
            output.append(f"{name}={replacements[name]}")
            found.add(name)
        else:
            output.append(line)
    for name in replacements.keys() - found:
        output.append(f"{name}={replacements[name]}")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target
