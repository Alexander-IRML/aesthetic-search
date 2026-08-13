from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from artsearch.ingest.db import connect, init_db
from artsearch.production.manifests import (
    build_corpus_manifest,
    build_intake_metrics,
    manifest_summary,
)


def test_build_manifest_keeps_latest_source_version_and_builds_metrics(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    decisions = tmp_path / "decisions.jsonl"
    manifest = tmp_path / "manifest.parquet"
    metrics = tmp_path / "metrics.parquet"
    database = tmp_path / "artsearch.db"
    _write_jsonl(
        candidates,
        [
            _candidate("one", "cid-1", "did:plc:artist-one"),
            _candidate("two", "cid-2", "did:plc:artist-two"),
        ],
    )
    _write_jsonl(
        decisions,
        [
            _decision("one", "cid-1", "review", "2026-08-11T10:00:00Z"),
            _decision("one", "cid-1", "accept", "2026-08-12T10:00:00Z"),
            _decision("two", "cid-2", "reject", "2026-08-12T11:00:00Z"),
        ],
    )
    _insert_published_object(database)

    result = build_corpus_manifest(
        candidates,
        decisions,
        manifest,
        database_path=database,
    )
    metric_result = build_intake_metrics(manifest, metrics)
    frame = pl.read_parquet(manifest).sort("candidate_id")

    assert result.row_count == 2
    assert result.accepted_count == 1
    assert result.rejected_count == 1
    assert frame["decision"].to_list() == ["accept", "reject"]
    assert frame["model_revision"].to_list() == ["model-revision", "model-revision"]
    assert frame["is_search_eligible"].to_list() == [True, False]
    assert frame["object_store_uri"].to_list() == [
        "s3://bucket/artsearch/corpus/one.jpg",
        None,
    ]
    assert frame["original_sha256"].to_list() == ["a" * 64, None]
    assert metric_result.row_count == 2
    assert manifest_summary(manifest)["rows"] == 2


def test_empty_run_produces_valid_empty_parquet_products(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    decisions = tmp_path / "decisions.jsonl"
    manifest = tmp_path / "manifest.parquet"
    metrics = tmp_path / "metrics.parquet"
    candidates.touch()
    decisions.touch()

    result = build_corpus_manifest(candidates, decisions, manifest)
    metric_result = build_intake_metrics(manifest, metrics)

    assert result.row_count == 0
    assert pl.read_parquet(manifest).is_empty()
    assert "object_store_uri" in pl.read_parquet_schema(manifest)
    assert metric_result.row_count == 0


def _candidate(candidate_id: str, cid: str, did: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "author_did": did,
        "author_handle": f"{candidate_id}.bsky.social",
        "post_uri": f"at://{did}/app.bsky.feed.post/{candidate_id}",
        "post_cid": cid,
        "image_index": 0,
        "thumbnail_url": f"https://cdn.example/{candidate_id}-thumb.jpg",
        "fullsize_url": f"https://cdn.example/{candidate_id}.jpg",
        "local_path": None,
        "post_text": "art",
        "alt_text": "illustration",
        "created_at": "2026-08-10T09:00:00Z",
        "langs": ["en"],
        "content_labels": [],
        "author_labels": [],
        "is_repost": False,
        "is_quote_post": False,
        "quoted_author_did": None,
        "declared_width": 1000,
        "declared_height": 1000,
        "mime_type": "image/jpeg",
        "source": "bluesky",
    }


def _decision(
    candidate_id: str,
    cid: str,
    decision: str,
    processed_at: str,
) -> dict[str, object]:
    accepted = decision == "accept"
    return {
        "candidate_id": candidate_id,
        "decision": decision,
        "predicted_class": "finished_illustration" if accepted else "casual_photo",
        "accepted_for_main_corpus": accepted,
        "route": "main_art" if accepted else "rejected",
        "final_score": 0.9 if accepted else 0.1,
        "confidence": 0.95,
        "reason_codes": [f"{decision}.test"],
        "image_sha256": ("1" if candidate_id == "one" else "2") * 64,
        "width": 1000,
        "height": 1000,
        "visual_scores": {
            "backend": "siglip2",
            "model_id": "google/siglip2-base-patch16-224",
            "model_revision": "model-revision",
            "mode": "zero_shot",
            "class_scores": [],
            "art_utility_score": 0.9 if accepted else 0.1,
            "noise_score": 0.1 if accepted else 0.9,
            "confidence_margin": 0.8,
            "embedding_dimension": 768,
            "embedding_cache_key": "cache-key",
        },
        "text_scores": None,
        "rule_result": None,
        "model_version": "google/siglip2-base-patch16-224",
        "config_version": "1.0.0",
        "prompt_version": "v1",
        "classifier_version": None,
        "processed_at": processed_at,
        "duration_ms": 20.0,
        "error_type": None,
        "error_message": None,
        "source_uri": f"at://did:plc:test/app.bsky.feed.post/{candidate_id}",
        "source_cid": cid,
        "author_did": "did:plc:test",
        "image_index": 0,
        "config_hash": "config-hash",
        "software_version": "0.3.0",
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _insert_published_object(database: Path) -> None:
    with connect(database) as connection:
        init_db(connection)
        connection.execute(
            """
            INSERT INTO artists (artist_id, display_name, folder_name, source_platform)
            VALUES ('artist-one', 'One', 'one', 'bluesky')
            """
        )
        connection.execute(
            """
            INSERT INTO artworks (
                artwork_id, artist_id, raw_path, source_platform, source_id,
                file_hash, validated
            ) VALUES ('artwork-one', 'artist-one', 'data/raw/one.jpg', 'bluesky',
                      'one', ?, 1)
            """,
            ("a" * 64,),
        )
        connection.execute(
            """
            INSERT INTO artwork_objects (
                artwork_id, role, object_key, object_uri, content_sha256,
                byte_size, etag
            ) VALUES ('artwork-one', 'original', 'corpus/one.jpg',
                      's3://bucket/artsearch/corpus/one.jpg', ?, 123, 'etag')
            """,
            ("a" * 64,),
        )
        connection.commit()
