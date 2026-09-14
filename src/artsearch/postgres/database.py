from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import parse_qs, urlsplit, urlunsplit

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from artsearch.production.config import PostgresConfig


class PostgresConfigurationError(RuntimeError):
    """Raised when a safe PostgreSQL connection cannot be constructed."""


def resolve_database_url(
    config: PostgresConfig,
    *,
    direct: bool = False,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve a secret URL and select psycopg 3 explicitly."""

    environment = os.environ if environ is None else environ
    variable = config.direct_url_env if direct else config.database_url_env
    value = environment.get(variable, "").strip()
    if not value:
        kind = "direct migration" if direct else "pooled runtime"
        raise PostgresConfigurationError(f"{kind} PostgreSQL URL is missing from {variable}")
    if value.startswith("postgres://"):
        value = "postgresql://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value.removeprefix("postgresql://")
    if not value.startswith("postgresql+psycopg://"):
        raise PostgresConfigurationError(
            f"{variable} must be a PostgreSQL URL compatible with psycopg 3"
        )
    if config.require_tls:
        sslmode = parse_qs(urlsplit(value).query).get("sslmode", [""])[0]
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            raise PostgresConfigurationError(
                f"{variable} must set sslmode=require, verify-ca, or verify-full"
            )
    return value


def redact_database_url(value: str) -> str:
    """Return a connection identifier that is safe to print or log."""

    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    username = f"{parsed.username}@" if parsed.username else ""
    return urlunsplit((parsed.scheme, f"{username}{hostname}{port}", parsed.path, "", ""))


def build_engine(
    config: PostgresConfig,
    *,
    direct: bool = False,
    environ: Mapping[str, str] | None = None,
    echo: bool = False,
) -> Engine:
    """Build a bounded sync engine for runtime or direct maintenance work."""

    url = resolve_database_url(config, direct=direct, environ=environ)
    connect_args = {"application_name": config.application_name}
    if direct:
        connect_args["options"] = f"-c statement_timeout={config.statement_timeout_ms}"
    common: dict[str, object] = {
        "pool_pre_ping": True,
        "connect_args": connect_args,
        "echo": echo,
    }
    if direct:
        common.update(pool_size=1, max_overflow=0, pool_timeout=config.pool_timeout_seconds)
    else:
        common.update(
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_timeout=config.pool_timeout_seconds,
        )
    engine = create_engine(url, **common)
    if not direct:
        timeout_ms = config.statement_timeout_ms

        @event.listens_for(engine, "begin")
        def _set_local_statement_timeout(connection: Connection) -> None:
            # Neon uses transaction-mode PgBouncer and rejects this as a
            # startup option. SET LOCAL keeps the limit scoped to one checkout.
            connection.exec_driver_sql(
                f"SET LOCAL statement_timeout = {timeout_ms}",
                execution_options={"stream_results": False},
            )

    return engine


def build_async_engine(
    config: PostgresConfig,
    *,
    direct: bool = False,
    environ: Mapping[str, str] | None = None,
    echo: bool = False,
) -> AsyncEngine:
    """Build a bounded async engine for latency-sensitive serving I/O."""

    url = resolve_database_url(config, direct=direct, environ=environ)
    connect_args = {"application_name": config.application_name}
    if direct:
        connect_args["options"] = f"-c statement_timeout={config.statement_timeout_ms}"
    common: dict[str, object] = {
        "pool_pre_ping": True,
        "connect_args": connect_args,
        "echo": echo,
    }
    if direct:
        common.update(pool_size=1, max_overflow=0, pool_timeout=config.pool_timeout_seconds)
    else:
        common.update(
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_timeout=config.pool_timeout_seconds,
        )
    engine = create_async_engine(url, **common)
    if not direct:
        timeout_ms = config.statement_timeout_ms

        @event.listens_for(engine.sync_engine, "begin")
        def _set_local_statement_timeout(connection: Connection) -> None:
            connection.exec_driver_sql(
                f"SET LOCAL statement_timeout = {timeout_ms}",
                execution_options={"stream_results": False},
            )

    return engine


@contextmanager
def rls_scope(
    connection: Connection,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> Iterator[Connection]:
    """Set transaction-local identity values consumed by product RLS policies."""

    connection.execute(
        text("SELECT set_config('app.user_id', :value, true)"),
        {"value": user_id or ""},
    )
    connection.execute(
        text("SELECT set_config('app.session_id', :value, true)"),
        {"value": session_id or ""},
    )
    yield connection


def database_health(engine: Engine) -> dict[str, object]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT current_database(), current_user, current_setting('server_version_num')")
        ).one()
    return {
        "database": row[0],
        "user": row[1],
        "server_version_num": int(row[2]),
    }
