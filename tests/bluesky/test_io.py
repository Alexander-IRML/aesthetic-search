import json

from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.bluesky.io import (
    JSONLActorCheckpointStore,
    JSONLCandidateStore,
    actor_checkpoint_key,
    write_candidates_jsonl,
)


def test_write_candidates_jsonl_serializes_candidate_rows(tmp_path):
    output_path = tmp_path / "candidates.jsonl"
    candidate = ImageCandidate(
        candidate_id="candidate",
        thumbnail_url="https://cdn.example/thumb.jpg",
        source="bluesky",
    )

    count = write_candidates_jsonl(output_path, [candidate])

    assert count == 1
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["candidate_id"] == "candidate"
    assert rows[0]["thumbnail_url"] == "https://cdn.example/thumb.jpg"


def test_atomic_candidate_replacement_preserves_old_file_until_commit(tmp_path):
    output_path = tmp_path / "candidates.jsonl"
    output_path.write_text("old-record\n", encoding="utf-8")
    store = JSONLCandidateStore(output_path, append=False)

    assert output_path.read_text(encoding="utf-8") == "old-record\n"
    assert store.commit() is False
    assert output_path.read_text(encoding="utf-8") == "old-record\n"

    candidate = ImageCandidate(
        candidate_id="candidate",
        thumbnail_url="https://cdn.example/thumb.jpg",
        source="test",
    )
    store.append_many([candidate])
    assert output_path.read_text(encoding="utf-8") == "old-record\n"

    assert store.commit() is True
    rows = output_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["candidate_id"] == "candidate"


def test_successful_empty_candidate_replacement_can_publish_empty_file(tmp_path):
    output_path = tmp_path / "candidates.jsonl"
    output_path.write_text("old-record\n", encoding="utf-8")
    store = JSONLCandidateStore(output_path, append=False)

    assert store.commit(allow_empty=True) is True
    assert output_path.read_text(encoding="utf-8") == ""


def test_append_candidate_store_deduplicates_versions_but_keeps_post_edits(tmp_path):
    output_path = tmp_path / "candidates.jsonl"
    first = ImageCandidate(
        candidate_id="candidate",
        post_cid="cid-one",
        thumbnail_url="https://cdn.example/thumb.jpg",
        source="test",
    )
    write_candidates_jsonl(output_path, [first], append=True)
    store = JSONLCandidateStore(output_path, append=True)
    edited = first.model_copy(update={"post_cid": "cid-two"})

    assert store.append_many([first, edited, edited]) == 1

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert [(row["candidate_id"], row["post_cid"]) for row in rows] == [
        ("candidate", "cid-one"),
        ("candidate", "cid-two"),
    ]


def test_actor_checkpoint_is_scoped_to_effective_settings(tmp_path):
    path = tmp_path / "checkpoints.jsonl"
    settings = {"max_pages": 1, "limit": 100, "feed_filter": "posts_with_media"}
    key = actor_checkpoint_key("artist.example", settings)
    store = JSONLActorCheckpointStore(path)

    store.mark_completed(
        key=key,
        actor="artist.example",
        candidate_count=12,
        settings=settings,
    )

    reloaded = JSONLActorCheckpointStore(path)
    assert reloaded.is_completed(key)
    assert not reloaded.is_completed(
        actor_checkpoint_key("artist.example", {**settings, "max_pages": 2})
    )
