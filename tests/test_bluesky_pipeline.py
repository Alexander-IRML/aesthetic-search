import asyncio
import json
from types import SimpleNamespace

import pytest

import artsearch.bluesky_pipeline_cli as pipeline_cli
from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.bluesky.io import JSONLCandidateStore
from artsearch.bluesky_pipeline import BlueskyArtworkPipeline
from artsearch.bluesky_pipeline import PipelineSummary


class FakeService:
    def __init__(self):
        self.config = load_artwork_filter_config()
        self.config.model.batch_size = 2
        self.batch_sizes = []

    async def classify_many(self, candidates):
        self.batch_sizes.append(len(candidates))
        return [SimpleNamespace(decision=FilterDecision.REVIEW) for _ in candidates]


class FakeRouter:
    async def route_many(self, candidates, results):
        return []


class FakeCorpusStore:
    def persist_batch(self, candidates, results, routed):
        return {
            "imported": 0,
            "unchanged": 0,
            "duplicates": 0,
            "review_stored": 0,
            "errors": 0,
        }


def test_bluesky_pipeline_batches_stream_and_records_candidates(tmp_path):
    service = FakeService()
    candidate_store = JSONLCandidateStore(tmp_path / "candidates.jsonl", append=False)
    pipeline = BlueskyArtworkPipeline(
        service,
        FakeRouter(),
        FakeCorpusStore(),
        candidate_store=candidate_store,
    )

    async def candidates():
        for index in range(3):
            yield ImageCandidate(
                candidate_id=f"candidate-{index}",
                fullsize_url="https://example.com/image.jpg",
                source="test",
            )

    summary = asyncio.run(pipeline.process_stream(candidates()))

    assert service.batch_sizes == [2, 1]
    assert summary.candidates == 3
    assert summary.review == 3
    assert len((tmp_path / "candidates.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_bluesky_pipeline_flushes_trailing_candidates_when_source_fails(tmp_path):
    service = FakeService()
    candidate_path = tmp_path / "candidates.jsonl"
    pipeline = BlueskyArtworkPipeline(
        service,
        FakeRouter(),
        FakeCorpusStore(),
        candidate_store=JSONLCandidateStore(candidate_path, append=False),
    )
    summary = PipelineSummary()

    async def candidates():
        yield ImageCandidate(
            candidate_id="candidate-before-error",
            fullsize_url="https://example.com/image.jpg",
            source="test",
        )
        raise RuntimeError("source stopped")

    with pytest.raises(RuntimeError, match="source stopped"):
        asyncio.run(pipeline.process_stream(candidates(), summary=summary))

    assert summary.candidates == 1
    assert summary.review == 1
    rows = candidate_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(rows[0])["candidate_id"] == "candidate-before-error"


def test_bluesky_pipeline_skips_duplicate_candidate_versions_in_live_stream(tmp_path):
    service = FakeService()
    pipeline = BlueskyArtworkPipeline(
        service,
        FakeRouter(),
        FakeCorpusStore(),
        candidate_store=JSONLCandidateStore(tmp_path / "candidates.jsonl", append=False),
    )

    async def candidates():
        for _ in range(2):
            yield ImageCandidate(
                candidate_id="candidate",
                post_cid="cid-one",
                fullsize_url="https://example.com/image.jpg",
                source="test",
            )

    summary = asyncio.run(pipeline.process_stream(candidates()))

    assert service.batch_sizes == [1]
    assert summary.candidates == 1
    assert summary.skipped == 1


def test_pipeline_cli_exits_nonzero_when_summary_contains_errors(monkeypatch, capsys):
    async def fake_run(args):
        return PipelineSummary(classification_errors=1)

    monkeypatch.setattr(pipeline_cli, "_run", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        ["artsearch-bluesky-pipeline", "run-author", "--actor", "artist.example"],
    )

    with pytest.raises(SystemExit) as raised:
        pipeline_cli.main()

    assert raised.value.code == 1
    assert json.loads(capsys.readouterr().out)["classification_errors"] == 1
