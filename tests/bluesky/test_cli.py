import argparse
import asyncio
import json

from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.bluesky import cli
from artsearch.bluesky.client import BlueskyAPIError


class FakeClient:
    def __init__(self, config):
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None


def test_collect_authors_continues_after_bad_actor_and_checkpoints_success(
    tmp_path,
    monkeypatch,
):
    actors_path = tmp_path / "actors.txt"
    actors_path.write_text("missing.example\nworking.example\n", encoding="utf-8")
    output_path = tmp_path / "candidates.jsonl"
    output_path.write_text("old-record\n", encoding="utf-8")
    checkpoint_path = tmp_path / "checkpoints.jsonl"

    async def fake_candidates(client, actor, **kwargs):
        if actor == "missing.example":
            raise BlueskyAPIError("profile missing", status_code=400)
        yield ImageCandidate(
            candidate_id="working-candidate",
            thumbnail_url="https://cdn.example/image.jpg",
            source="test",
        )

    monkeypatch.setattr(cli, "BlueskyClient", FakeClient)
    monkeypatch.setattr(cli, "iter_author_image_candidates", fake_candidates)
    args = argparse.Namespace(
        command="collect-authors",
        config="configs/bluesky.default.toml",
        output=str(output_path),
        checkpoint=str(checkpoint_path),
        actors_file=str(actors_path),
        max_pages=1,
        limit=None,
        feed_filter=None,
        append=False,
        resume=False,
        fail_fast=False,
    )

    summary = asyncio.run(cli._collect(args))

    assert summary.actor_errors == 1
    assert summary.actors_completed == 1
    assert summary.candidates == 1
    rows = output_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(rows[0])["candidate_id"] == "working-candidate"
    assert len(checkpoint_path.read_text(encoding="utf-8").splitlines()) == 1


def test_failed_first_actor_does_not_erase_existing_candidate_file(tmp_path, monkeypatch):
    output_path = tmp_path / "candidates.jsonl"
    output_path.write_text("old-record\n", encoding="utf-8")

    async def fake_candidates(client, actor, **kwargs):
        if False:
            yield
        raise BlueskyAPIError("profile missing", status_code=400)

    monkeypatch.setattr(cli, "BlueskyClient", FakeClient)
    monkeypatch.setattr(cli, "iter_author_image_candidates", fake_candidates)
    args = argparse.Namespace(
        command="collect-author",
        config="configs/bluesky.default.toml",
        output=str(output_path),
        checkpoint=str(tmp_path / "checkpoints.jsonl"),
        actor="missing.example",
        max_pages=1,
        limit=None,
        feed_filter=None,
        append=False,
        resume=False,
        fail_fast=False,
    )

    summary = asyncio.run(cli._collect(args))

    assert summary.actor_errors == 1
    assert output_path.read_text(encoding="utf-8") == "old-record\n"
