from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import threading
import time

from artsearch.ingest.db import connect, init_db
from artsearch.production.object_store import LocalObjectStore, ObjectRef
from artsearch.production.publisher import candidate_ids_from_jsonl, publish_corpus_originals


def test_publish_corpus_originals_checkpoints_object_uri(tmp_path: Path) -> None:
    raw = tmp_path / "data/raw/artist/image.jpg"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"accepted image")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    database = tmp_path / "data/artsearch.db"
    with connect(database) as connection:
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
                artwork_id, artist_id, raw_path, source_platform, source_id,
                file_hash, validated
            ) VALUES ('artwork', 'artist', 'data/raw/artist/image.jpg', 'bluesky',
                      'candidate', ?, 1)
            """,
            (digest,),
        )
        connection.commit()

    store = LocalObjectStore(tmp_path / "objects")
    first = publish_corpus_originals(
        database,
        tmp_path,
        store,
        candidate_ids={"candidate"},
    )
    second = publish_corpus_originals(
        database,
        tmp_path,
        store,
        candidate_ids={"candidate"},
    )

    assert first.published == 1
    assert first.failed == 0
    assert second.unchanged == 1
    with connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM artwork_objects WHERE artwork_id = 'artwork'"
        ).fetchone()
    assert row is not None
    assert row["content_sha256"] == digest
    assert row["object_uri"].startswith("file://")

    Path(row["object_uri"].removeprefix("file://")).unlink()
    repaired = publish_corpus_originals(
        database,
        tmp_path,
        store,
        candidate_ids={"candidate"},
    )
    assert repaired.published == 1


def test_candidate_ids_from_jsonl_deduplicates_run_ids(tmp_path: Path) -> None:
    path = tmp_path / "candidates.jsonl"
    path.write_text(
        '{"candidate_id": "one"}\n{"candidate_id": "two"}\n{"candidate_id": "one"}\n',
        encoding="utf-8",
    )

    assert candidate_ids_from_jsonl(path) == {"one", "two"}


def test_candidate_ids_from_jsonl_can_select_accepts(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"candidate_id": "one", "decision": "accept"}\n'
        '{"candidate_id": "two", "decision": "review"}\n',
        encoding="utf-8",
    )

    assert candidate_ids_from_jsonl(path, required_decision="accept") == {"one"}


def test_publish_scope_follows_duplicate_route_to_canonical_artwork(tmp_path: Path) -> None:
    raw = tmp_path / "data/raw/canonical.jpg"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"canonical duplicate")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    database = tmp_path / "data/artsearch.db"
    with connect(database) as connection:
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
                artwork_id, artist_id, raw_path, source_platform, source_id,
                file_hash, validated
            ) VALUES ('canonical', 'artist', 'data/raw/canonical.jpg', 'bluesky',
                      'original-candidate', ?, 1)
            """,
            (digest,),
        )
        _insert_decision_and_duplicate_route(connection, digest)
        connection.commit()

    result = publish_corpus_originals(
        database,
        tmp_path,
        LocalObjectStore(tmp_path / "objects"),
        candidate_ids={"duplicate-candidate"},
    )

    assert result.eligible == 1
    assert result.published == 1


def test_publish_corpus_uses_bounded_parallel_transfers_and_batch_checkpoints(
    tmp_path: Path,
) -> None:
    database = tmp_path / "data/artsearch.db"
    _insert_test_artworks(database, tmp_path, count=8)
    store = TrackingObjectStore(tmp_path / "objects", delay_seconds=0.02)

    result = publish_corpus_originals(
        database,
        tmp_path,
        store,
        upload_concurrency=4,
        checkpoint_batch_size=3,
        retry_base_seconds=0,
    )

    assert result.published == 8
    assert result.failed == 0
    assert result.upload_concurrency == 4
    assert result.bytes_uploaded > 0
    assert result.objects_per_second > 0
    assert store.max_active > 1
    with connect(database) as connection:
        published = connection.execute("SELECT COUNT(*) FROM artwork_objects").fetchone()[0]
    assert published == 8


def test_publish_corpus_retries_transient_object_store_failures(tmp_path: Path) -> None:
    database = tmp_path / "data/artsearch.db"
    _insert_test_artworks(database, tmp_path, count=1)
    store = TrackingObjectStore(tmp_path / "objects", failures=2)

    result = publish_corpus_originals(
        database,
        tmp_path,
        store,
        max_attempts=3,
        retry_base_seconds=0,
    )

    assert result.published == 1
    assert result.failed == 0
    assert store.put_calls == 3


def _insert_decision_and_duplicate_route(
    connection: sqlite3.Connection,
    digest: str,
) -> None:
    connection.execute(
        """
        INSERT INTO artwork_filter_decisions (
            decision_key, candidate_id, image_sha256, decision, predicted_class,
            accepted_for_main_corpus, route, final_score, confidence,
            reason_codes_json, candidate_json, evidence_json, model_id,
            config_version, config_hash, software_version, processed_at, duration_ms
        ) VALUES (
            'decision', 'duplicate-candidate', ?, 'accept', 'finished_illustration',
            1, 'main_art', 0.9, 0.9, '[]', '{}', '{}', 'model',
            'config', 'hash', 'software', '2026-08-13T00:00:00Z', 1.0
        )
        """,
        (digest,),
    )
    connection.execute(
        """
        INSERT INTO artwork_filter_routes (
            route_key, decision_key, candidate_id, target, status, artwork_id
        ) VALUES (
            'route', 'decision', 'duplicate-candidate', 'corpus', 'duplicate', 'canonical'
        )
        """
    )


def _insert_test_artworks(database: Path, root: Path, *, count: int) -> None:
    with connect(database) as connection:
        init_db(connection)
        connection.execute(
            """
            INSERT INTO artists (artist_id, display_name, folder_name, source_platform)
            VALUES ('parallel-artist', 'Parallel Artist', 'parallel-artist', 'bluesky')
            """
        )
        for index in range(count):
            path = root / f"data/raw/parallel-{index}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"parallel image {index}".encode())
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            connection.execute(
                """
                INSERT INTO artworks (
                    artwork_id, artist_id, raw_path, source_platform, source_id,
                    file_hash, validated
                ) VALUES (?, 'parallel-artist', ?, 'bluesky', ?, ?, 1)
                """,
                (
                    f"parallel-{index}",
                    str(path.relative_to(root)),
                    f"candidate-{index}",
                    digest,
                ),
            )
        connection.commit()


class TrackingObjectStore:
    def __init__(
        self,
        root: Path,
        *,
        delay_seconds: float = 0,
        failures: int = 0,
    ) -> None:
        self.delegate = LocalObjectStore(root)
        self.delay_seconds = delay_seconds
        self.failures = failures
        self.put_calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def put_file(
        self,
        source: str | Path,
        key: str,
        *,
        expected_sha256: str | None = None,
    ) -> ObjectRef:
        with self.lock:
            self.put_calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            should_fail = self.failures > 0
            if should_fail:
                self.failures -= 1
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            if should_fail:
                raise RuntimeError("transient object-store failure")
            return self.delegate.put_file(
                source,
                key,
                expected_sha256=expected_sha256,
            )
        finally:
            with self.lock:
                self.active -= 1

    def get_file(self, key: str, destination: str | Path) -> ObjectRef:
        return self.delegate.get_file(key, destination)

    def exists(self, key: str) -> bool:
        return self.delegate.exists(key)

    def delete(self, key: str) -> bool:
        return self.delegate.delete(key)
