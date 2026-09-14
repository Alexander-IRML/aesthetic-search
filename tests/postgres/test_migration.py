from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from artsearch.postgres import migration


def test_candidate_jsonl_scope_is_deduplicated_and_validated(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"candidate_id": "candidate-b"}),
                json.dumps({"candidate_id": "candidate-a"}),
                json.dumps({"candidate_id": "candidate-b"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert migration.candidate_ids_from_jsonl(path) == ["candidate-a", "candidate-b"]

    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(migration.SQLiteMigrationError, match="candidate_id"):
        migration.candidate_ids_from_jsonl(path)


def test_scope_and_import_keys_are_deterministic() -> None:
    first = migration._scope_metadata(["a", "b"])
    second = migration._scope_metadata(["a", "b"])

    assert first == second
    assert migration._import_key("0" * 64, first) == migration._import_key("0" * 64, second)
    assert migration._import_key("0" * 64, first) != migration._import_key("1" * 64, second)


def test_embedding_cutover_validates_dimension_and_finiteness() -> None:
    values = np.linspace(-1.0, 1.0, 512, dtype=np.float32)
    migrated = migration._vector(values.tobytes(), 512, expected=512, name="CLIP")

    assert migrated is not None
    assert len(migrated) == 512
    with pytest.raises(migration.SQLiteMigrationError, match="dimension"):
        migration._vector(values.tobytes(), 511, expected=512, name="CLIP")

    values[10] = np.nan
    with pytest.raises(migration.SQLiteMigrationError, match="non-finite"):
        migration._vector(values.tobytes(), 512, expected=512, name="CLIP")


def test_cutover_does_not_import_environment_specific_qdrant_state() -> None:
    source_tables = {spec.source_name for spec in migration._table_specs(scoped=False)}

    assert "vector_index_points" not in source_tables


def test_compact_cutover_omits_vectors_and_heavy_audit_history() -> None:
    source_tables = {
        spec.source_name
        for spec in migration._table_specs(
            scoped=False,
            include_embeddings=False,
            include_filter_history=False,
            include_run_history=False,
        )
    }

    assert "artists" in source_tables
    assert "artworks" in source_tables
    assert "artwork_objects" in source_tables
    assert "embeddings" not in source_tables
    assert "artwork_filter_decisions" not in source_tables
    assert "artwork_filter_routes" not in source_tables
    assert "runs" not in source_tables
    assert "run_events" not in source_tables
