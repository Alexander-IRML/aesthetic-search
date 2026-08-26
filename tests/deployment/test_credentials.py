from pathlib import Path

import pytest

from artsearch.deployment.credentials import (
    derive_neon_connection_urls,
    ensure_session_secret,
    write_neon_connection_string,
    write_postgres_credentials,
    write_qdrant_credentials,
)


def test_write_qdrant_credentials_is_private_and_preserves_other_values(
    tmp_path: Path,
) -> None:
    template = tmp_path / "example.env"
    destination = tmp_path / ".env"
    template.write_text(
        "AWS_DEFAULT_REGION=auto\nQDRANT_URL=\nQDRANT_API_KEY=\n",
        encoding="utf-8",
    )

    written = write_qdrant_credentials(
        destination,
        endpoint="https://cluster.cloud.qdrant.io:6333/",
        api_key="secret-key",
        template=template,
    )

    assert written == destination
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.read_text(encoding="utf-8") == (
        "AWS_DEFAULT_REGION=auto\n"
        "QDRANT_URL=https://cluster.cloud.qdrant.io:6333\n"
        "QDRANT_API_KEY=secret-key\n"
    )


def test_write_qdrant_credentials_rejects_non_https_endpoint(tmp_path: Path) -> None:
    template = tmp_path / "example.env"
    template.write_text("QDRANT_URL=\nQDRANT_API_KEY=\n", encoding="utf-8")

    with pytest.raises(ValueError, match="HTTPS"):
        write_qdrant_credentials(
            tmp_path / ".env",
            endpoint="http://localhost:6333",
            api_key="secret-key",
            template=template,
        )


def test_write_postgres_credentials_keeps_pooled_and_direct_urls_private(
    tmp_path: Path,
) -> None:
    template = tmp_path / "example.env"
    destination = tmp_path / ".env"
    template.write_text("DATABASE_URL=\nDATABASE_DIRECT_URL=\n", encoding="utf-8")

    write_postgres_credentials(
        destination,
        pooled_url="postgresql://app:secret@pooler.example/artsearch?sslmode=require",
        direct_url="postgresql://owner:secret@direct.example/artsearch?sslmode=require",
        template=template,
    )

    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.read_text(encoding="utf-8") == (
        "DATABASE_URL=postgresql://app:secret@pooler.example/artsearch?sslmode=require\n"
        "DATABASE_DIRECT_URL="
        "postgresql://owner:secret@direct.example/artsearch?sslmode=require\n"
    )


def test_write_postgres_credentials_rejects_same_or_incomplete_urls(tmp_path: Path) -> None:
    template = tmp_path / "example.env"
    template.write_text("", encoding="utf-8")
    valid = "postgresql://app:secret@example/artsearch"

    with pytest.raises(ValueError, match="must differ"):
        write_postgres_credentials(
            tmp_path / ".env",
            pooled_url=valid,
            direct_url=valid,
            template=template,
        )
    with pytest.raises(ValueError, match="user, password, and database"):
        write_postgres_credentials(
            tmp_path / ".env",
            pooled_url="postgresql://example/artsearch",
            direct_url=valid,
            template=template,
        )


def test_derive_neon_connection_urls_from_default_pooled_url() -> None:
    connection = (
        "postgresql://app:secret@ep-example-123-pooler.us-west-2.aws.neon.tech/"
        "artsearch?sslmode=require&channel_binding=require"
    )

    pooled, direct = derive_neon_connection_urls(connection)

    assert pooled == connection
    assert direct == connection.replace("-pooler.us-west-2", ".us-west-2")


def test_write_neon_connection_string_derives_pooled_url_from_direct(
    tmp_path: Path,
) -> None:
    template = tmp_path / "example.env"
    destination = tmp_path / ".env"
    template.write_text("DATABASE_URL=\nDATABASE_DIRECT_URL=\n", encoding="utf-8")
    direct = (
        "postgresql://app:secret@ep-example-123.us-west-2.aws.neon.tech/artsearch?sslmode=require"
    )

    write_neon_connection_string(
        destination,
        connection_url=direct,
        template=template,
    )

    assert destination.read_text(encoding="utf-8") == (
        "DATABASE_URL="
        "postgresql://app:secret@ep-example-123-pooler.us-west-2.aws.neon.tech/"
        "artsearch?sslmode=require\n"
        f"DATABASE_DIRECT_URL={direct}\n"
    )


def test_derive_neon_connection_urls_rejects_non_neon_host() -> None:
    with pytest.raises(ValueError, match="neon.tech"):
        derive_neon_connection_urls(
            "postgresql://app:secret@postgres.example/artsearch?sslmode=require"
        )


def test_ensure_session_secret_generates_once_and_preserves_it(tmp_path: Path) -> None:
    template = tmp_path / "example.env"
    destination = tmp_path / ".env"
    template.write_text("QDRANT_URL=\nARTSEARCH_SESSION_SECRET=\n", encoding="utf-8")

    written, generated = ensure_session_secret(destination, template=template)
    first = written.read_text(encoding="utf-8")
    written_again, generated_again = ensure_session_secret(destination, template=template)

    assert generated is True
    assert generated_again is False
    assert written_again == destination
    assert written_again.read_text(encoding="utf-8") == first
    secret = first.split("ARTSEARCH_SESSION_SECRET=", 1)[1].strip()
    assert len(secret.encode("utf-8")) >= 32
    assert destination.stat().st_mode & 0o777 == 0o600


def test_ensure_session_secret_rejects_existing_short_value(tmp_path: Path) -> None:
    destination = tmp_path / ".env"
    destination.write_text("ARTSEARCH_SESSION_SECRET=short\n", encoding="utf-8")

    with pytest.raises(ValueError, match="shorter than 32 bytes"):
        ensure_session_secret(destination, template=destination)
