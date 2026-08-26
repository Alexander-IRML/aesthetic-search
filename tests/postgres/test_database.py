from __future__ import annotations

import uuid

import pytest

from artsearch.postgres.database import (
    PostgresConfigurationError,
    redact_database_url,
    resolve_database_url,
)
from artsearch.postgres.ids import uuid7
from artsearch.production.config import PostgresConfig


def test_uuid7_has_expected_version_variant_and_timestamp_order() -> None:
    earlier = uuid7(timestamp_ms=1_700_000_000_000, random_bits=0)
    later = uuid7(timestamp_ms=1_700_000_000_001, random_bits=0)

    assert earlier.version == 7
    assert earlier.variant == uuid.RFC_4122
    assert earlier < later


def test_uuid7_rejects_fields_that_do_not_fit() -> None:
    with pytest.raises(ValueError, match="48-bit"):
        uuid7(timestamp_ms=1 << 48)
    with pytest.raises(ValueError, match="74 bits"):
        uuid7(random_bits=1 << 74)


def test_database_urls_select_psycopg_and_keep_direct_endpoint_separate() -> None:
    config = PostgresConfig()
    environment = {
        "DATABASE_URL": "postgresql://runtime:secret@pooler.example/artsearch?sslmode=require",
        "DATABASE_DIRECT_URL": "postgres://migration:secret@direct.example/artsearch",
    }

    pooled = resolve_database_url(config, environ=environment)
    direct = resolve_database_url(config, direct=True, environ=environment)

    assert pooled.startswith("postgresql+psycopg://runtime:secret@pooler.example")
    assert direct.startswith("postgresql+psycopg://migration:secret@direct.example")
    assert "secret" not in redact_database_url(pooled)
    assert "sslmode" not in redact_database_url(pooled)


def test_missing_database_url_names_the_required_secret() -> None:
    with pytest.raises(PostgresConfigurationError, match="DATABASE_DIRECT_URL"):
        resolve_database_url(PostgresConfig(), direct=True, environ={})


def test_cloud_database_url_requires_explicit_tls_mode() -> None:
    config = PostgresConfig(require_tls=True)
    with pytest.raises(PostgresConfigurationError, match="sslmode"):
        resolve_database_url(
            config,
            environ={"DATABASE_URL": "postgresql://app:secret@example/artsearch"},
        )
