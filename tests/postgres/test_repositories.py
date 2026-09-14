from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy.dialects import postgresql

from artsearch.ingest.config import ModelConfig
from artsearch.postgres.model_lineage import (
    configured_model_bundle_record,
    model_bundle_record,
)
from artsearch.postgres.repositories import OutboxRepository
from artsearch.production.config import QdrantConfig


class _Result:
    rowcount = 1

    def mappings(self) -> "_Result":
        return self

    def __iter__(self) -> Any:
        return iter(())

    def scalar_one(self) -> uuid.UUID:
        return uuid.UUID("01922222-2222-7222-8222-222222222222")


class _Connection:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any, parameters: Any = None) -> _Result:
        self.statements.append(statement)
        return _Result()


class _Transaction:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(self, *_: object) -> None:
        return None


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()

    def begin(self) -> _Transaction:
        return _Transaction(self.connection)


def test_outbox_claim_compiles_to_skip_locked_postgres_statement() -> None:
    engine = _Engine()
    repository = OutboxRepository(engine)  # type: ignore[arg-type]

    assert repository.claim("worker", event_type="qdrant.reconcile_artwork") == []
    compiled = str(engine.connection.statements[0].compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "UPDATE ingest.outbox_events" in compiled
    assert "event_type" in compiled


def test_outbox_failure_statement_has_bounded_retry_transition() -> None:
    engine = _Engine()
    repository = OutboxRepository(engine)  # type: ignore[arg-type]

    assert repository.mark_failed(uuid.uuid4(), "worker", "failure") is True
    compiled = str(engine.connection.statements[0].compile(dialect=postgresql.dialect()))

    assert "CASE WHEN" in compiled
    assert "make_interval" in compiled
    assert "status" in compiled


def test_model_bundle_lineage_matches_qdrant_contract() -> None:
    models = ModelConfig(
        clip_model_name="clip",
        clip_model_version="clip-revision",
        csd_model_name="csd",
        csd_model_version="csd-revision",
        csd_model_filename="model.safetensors",
        csd_model_sha256="0" * 64,
        csd_processor_name="processor",
        csd_processor_version="processor-revision",
    )
    qdrant = QdrantConfig(clip_dimension=512, csd_dimension=768)

    record = model_bundle_record(models, qdrant)

    assert len(record["model_bundle_version"]) == 64
    assert record["clip_model_revision"] == "clip-revision"
    assert record["csd_model_revision"] == "csd-revision"
    assert record["metadata"]["qdrant_collection"] == qdrant.collection_name


def test_configured_model_bundle_uses_faiss_for_scale_profile() -> None:
    from artsearch.production.config import ProductionConfig

    models = ModelConfig(
        clip_model_name="clip",
        clip_model_version="clip-revision",
        csd_model_name="csd",
        csd_model_version="csd-revision",
        csd_model_filename="model.safetensors",
        csd_model_sha256="0" * 64,
        csd_processor_name="processor",
        csd_processor_version="processor-revision",
    )
    production = ProductionConfig.model_validate(
        {"qdrant": {"enabled": False}, "faiss": {"enabled": True}}
    )

    record = configured_model_bundle_record(models, production)

    assert record["metadata"]["vector_backend"] == "faiss"
    assert record["clip_dimension"] == 512
    assert record["csd_dimension"] == 768


def test_browser_session_resolution_uses_postgres_upsert() -> None:
    from artsearch.postgres.repositories import TelemetryRepository

    engine = _Engine()
    repository = TelemetryRepository(engine)  # type: ignore[arg-type]

    session_id = repository.resolve_session("browser-session-1234")
    compiled = str(engine.connection.statements[0].compile(dialect=postgresql.dialect()))

    assert str(session_id) == "01922222-2222-7222-8222-222222222222"
    assert "ON CONFLICT (client_session_key) DO UPDATE" in compiled
    assert "last_seen_at" in compiled
    assert "client_metadata || excluded.client_metadata" in compiled
