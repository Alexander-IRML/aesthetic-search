from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import numpy as np

from artsearch.embed.storage import ImageEmbeddings, upsert_embedding
from artsearch.ingest.config import load_config
from artsearch.ingest.db import connect, init_db
from artsearch.production.config import LocalRetentionConfig
from artsearch.production.retention import apply_local_image_retention


ROOT = Path(__file__).resolve().parents[2]


def test_local_retention_is_dry_run_by_default_and_requires_current_vectors(
    tmp_path: Path,
) -> None:
    base = load_config(ROOT / "config/config.yaml")
    app = replace(
        base,
        root_dir=tmp_path,
        raw_dir=tmp_path / "data/raw",
        processed_dir=tmp_path / "data/processed",
        database_path=tmp_path / "data/artsearch.db",
    )
    raw = app.raw_dir / "artist/art.jpg"
    processed = app.processed_dir / "artist/art.jpg"
    raw.parent.mkdir(parents=True)
    processed.parent.mkdir(parents=True)
    raw.write_bytes(b"raw image")
    processed.write_bytes(b"processed image")
    with connect(app.database_path) as connection:
        init_db(connection)
        connection.execute(
            """
            INSERT INTO artists (artist_id, display_name, folder_name, source_platform)
            VALUES ('artist', 'Artist', 'artist', 'bluesky')
            """
        )
        connection.execute(
            """
            INSERT INTO artworks (
                artwork_id, artist_id, raw_path, processed_path, source_platform,
                source_id, validated
            ) VALUES ('art', 'artist', ?, ?, 'bluesky', 'candidate', 1)
            """,
            (str(raw.relative_to(tmp_path)), str(processed.relative_to(tmp_path))),
        )
        _insert_accept_decision(connection)
        upsert_embedding(
            connection,
            "art",
            ImageEmbeddings(
                clip_vector=np.ones(512, dtype=np.float32),
                csd_style=np.ones(768, dtype=np.float32),
            ),
            app.models,
        )
        connection.commit()

    policy = LocalRetentionConfig(enabled=True)
    planned = apply_local_image_retention(
        app.database_path,
        app,
        policy,
        candidate_ids={"candidate"},
    )

    assert planned.applied is False
    assert planned.files_selected == 2
    assert planned.files_deleted == 0
    assert raw.exists() and processed.exists()

    applied = apply_local_image_retention(
        app.database_path,
        app,
        policy,
        candidate_ids={"candidate"},
        apply=True,
    )

    assert applied.files_deleted == 2
    assert applied.bytes_deleted == len(b"raw image") + len(b"processed image")
    assert not raw.exists() and not processed.exists()


def test_disabled_retention_never_reports_an_applied_run(tmp_path: Path) -> None:
    base = load_config(ROOT / "config/config.yaml")
    app = replace(base, root_dir=tmp_path, database_path=tmp_path / "artsearch.db")

    result = apply_local_image_retention(
        app.database_path,
        app,
        LocalRetentionConfig(enabled=False),
        candidate_ids={"candidate"},
        apply=True,
    )

    assert result.applied is False
    assert result.files_deleted == 0


def _insert_accept_decision(connection) -> None:
    candidate = {
        "candidate_id": "candidate",
        "blob_cid": "bafy-source-blob",
        "fullsize_url": "https://cdn.bsky.app/img/feed_fullsize/plain/source@jpeg",
    }
    connection.execute(
        """
        INSERT INTO artwork_filter_decisions (
            decision_key, candidate_id, decision, predicted_class,
            accepted_for_main_corpus, route, final_score, confidence,
            reason_codes_json, candidate_json, evidence_json, model_id,
            config_version, config_hash, software_version, processed_at,
            duration_ms
        ) VALUES (
            'decision', 'candidate', 'accept', 'finished_illustration',
            1, 'main_art', 0.9, 0.9, '[]', ?, '{}', 'siglip',
            '1', 'hash', 'version', '2026-09-08T00:00:00Z', 1.0
        )
        """,
        (json.dumps(candidate),),
    )
