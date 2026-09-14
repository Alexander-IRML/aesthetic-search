from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy.dialects import postgresql

from artsearch.discovery.models import GraphEdgeType
from artsearch.discovery.repository import DiscoveryRepository
from artsearch.postgres.schema import (
    discovery_edge_evidence,
    discovery_edges,
    discovery_entities,
    discovery_frontier_tasks,
    discovery_media,
)


_EDGE_ID = uuid.UUID("01999999-9999-7999-8999-999999999999")
_EVIDENCE_ID = uuid.UUID("01988888-8888-7888-8888-888888888888")


class _Result:
    rowcount = 1

    def __init__(self, *, evidence_inserted: bool = True) -> None:
        self.evidence_inserted = evidence_inserted

    def scalar_one(self) -> uuid.UUID:
        return _EDGE_ID

    def scalar_one_or_none(self) -> uuid.UUID | None:
        return _EVIDENCE_ID if self.evidence_inserted else None

    def mappings(self) -> "_Result":
        return self

    def __iter__(self) -> Any:
        return iter(())


class _Connection:
    def __init__(self, *, evidence_inserted: bool = True) -> None:
        self.statements: list[Any] = []
        self.evidence_inserted = evidence_inserted

    def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(evidence_inserted=self.evidence_inserted)


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


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_frontier_claim_uses_skip_locked_and_can_claim_across_campaigns() -> None:
    engine = _Engine()
    repository = DiscoveryRepository(engine)  # type: ignore[arg-type]

    assert repository.claim_tasks("worker", limit=5, lease_seconds=30) == []
    compiled = _sql(engine.connection.statements[0])

    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "UPDATE discovery.frontier_tasks" in compiled
    assert "frontier_tasks.campaign_id =" not in compiled
    assert "locked_until" in compiled

    low_engine = _Engine()
    low_repository = DiscoveryRepository(low_engine)  # type: ignore[arg-type]
    assert (
        low_repository.claim_tasks(
            "worker",
            limit=1,
            lease_seconds=30,
            prefer_high_priority=False,
        )
        == []
    )
    assert "priority ASC" in _sql(low_engine.connection.statements[0])


def test_graph_edge_support_only_increments_for_new_evidence() -> None:
    repository = DiscoveryRepository(_Engine())  # type: ignore[arg-type]
    inserted = _Connection(evidence_inserted=True)
    duplicate = _Connection(evidence_inserted=False)
    arguments = {
        "source_entity_id": "creator:a",
        "target_entity_id": "creator:b",
        "edge_type": GraphEdgeType.FOLLOWS,
        "confidence": 0.75,
        "observation_id": None,
        "provenance_source": "test",
        "metadata": {"fixture": True},
        "evidence_ref": "record:one",
    }

    repository._record_edge(inserted, **arguments)
    repository._record_edge(duplicate, **arguments)

    assert len(inserted.statements) == 3
    assert len(duplicate.statements) == 2
    assert "support_count" not in _sql(inserted.statements[0])
    assert "ON CONFLICT (evidence_key) DO NOTHING" in _sql(inserted.statements[1])
    assert "support_count" in _sql(inserted.statements[2])
    first_key = inserted.statements[1].compile().params["evidence_key"]
    replay_key = duplicate.statements[1].compile().params["evidence_key"]
    assert first_key == replay_key


def test_discovery_schema_keeps_graph_evidence_and_stable_media_identity_fields() -> None:
    assert all(
        table.schema == "discovery"
        for table in (
            discovery_entities,
            discovery_edges,
            discovery_edge_evidence,
            discovery_frontier_tasks,
            discovery_media,
        )
    )
    assert discovery_edges.c.support_count.server_default is not None
    assert discovery_edges.c.support_count.server_default.arg.text == "0"
    assert discovery_edge_evidence.c.evidence_key.unique is True
    assert discovery_media.c.candidate_id.nullable is False
    assert discovery_media.c.blob_cid.nullable is True
