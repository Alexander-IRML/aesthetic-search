import asyncio
import json

from PIL import Image

from artsearch.artwork_filter.batch import classify_candidate_jsonl
from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.persistence import JSONLDecisionStore
from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.artwork_filter.service import ArtworkFilterService


def test_classify_candidate_jsonl_streams_batches_and_preserves_order(tmp_path):
    image_path = tmp_path / "image.jpg"
    Image.new("RGB", (512, 512), "white").save(image_path)
    candidates = [
        ImageCandidate(candidate_id=f"candidate-{index}", local_path=image_path, source="test")
        for index in range(3)
    ]
    input_path = tmp_path / "candidates.jsonl"
    input_path.write_text(
        "".join(candidate.model_dump_json() + "\n" for candidate in candidates),
        encoding="utf-8",
    )
    output_path = tmp_path / "decisions.jsonl"
    config = load_artwork_filter_config()
    config.model.batch_size = 2
    service = ArtworkFilterService(
        config,
        decision_store=JSONLDecisionStore(output_path, append=False),
    )

    counts = asyncio.run(classify_candidate_jsonl(service, input_path))

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert counts["processed"] == 3
    assert [row["candidate_id"] for row in rows] == [item.candidate_id for item in candidates]

    resumed = asyncio.run(
        classify_candidate_jsonl(
            service,
            input_path,
            resume_decisions_path=output_path,
        )
    )
    assert resumed["processed"] == 0
    assert resumed["skipped"] == 3


def test_resume_reclassifies_same_candidate_when_bluesky_post_cid_changes(tmp_path):
    image_path = tmp_path / "image.jpg"
    Image.new("RGB", (512, 512), "white").save(image_path)
    input_path = tmp_path / "candidates.jsonl"
    output_path = tmp_path / "decisions.jsonl"
    config = load_artwork_filter_config()
    service = ArtworkFilterService(
        config,
        decision_store=JSONLDecisionStore(output_path, append=False),
    )

    def write_candidate(post_cid):
        candidate = ImageCandidate(
            candidate_id="stable-candidate",
            post_uri="at://did:plc:test/app.bsky.feed.post/one",
            post_cid=post_cid,
            local_path=image_path,
            source="bluesky",
        )
        input_path.write_text(candidate.model_dump_json() + "\n", encoding="utf-8")

    write_candidate("cid-one")
    asyncio.run(classify_candidate_jsonl(service, input_path))
    write_candidate("cid-two")
    changed = asyncio.run(
        classify_candidate_jsonl(service, input_path, resume_decisions_path=output_path)
    )
    unchanged = asyncio.run(
        classify_candidate_jsonl(service, input_path, resume_decisions_path=output_path)
    )

    assert changed["processed"] == 1
    assert unchanged["processed"] == 0
    assert unchanged["skipped"] == 1
