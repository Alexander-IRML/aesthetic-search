import asyncio
import json
from pathlib import Path

from PIL import Image

from artsearch.artwork_filter.cli import main
from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.persistence import JSONLDecisionStore
from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.artwork_filter.service import ArtworkFilterService


def test_service_returns_review_for_valid_image_without_visual_model(tmp_path):
    image_path = _write_image(tmp_path / "image.jpg", size=(512, 512))
    config = load_artwork_filter_config()
    service = ArtworkFilterService(config)

    result = asyncio.run(
        service.classify(
            ImageCandidate(
                candidate_id="candidate",
                local_path=image_path,
                source="local",
                post_text="finished illustration",
            )
        )
    )

    assert result.decision == FilterDecision.REVIEW
    assert result.route == "review"
    assert result.image_sha256
    assert "review.no_visual_model" in result.reason_codes


def test_service_returns_error_for_corrupt_image(tmp_path):
    image_path = tmp_path / "bad.jpg"
    image_path.write_bytes(b"bad")
    config = load_artwork_filter_config()
    service = ArtworkFilterService(config)

    result = asyncio.run(
        service.classify(
            ImageCandidate(candidate_id="candidate", local_path=image_path, source="local")
        )
    )

    assert result.decision == FilterDecision.ERROR
    assert result.error_type == "ImageDecodeError"


def test_jsonl_decision_store_appends_results(tmp_path):
    image_path = _write_image(tmp_path / "image.jpg", size=(512, 512))
    output_path = tmp_path / "decisions.jsonl"
    config = load_artwork_filter_config()
    service = ArtworkFilterService(config, decision_store=JSONLDecisionStore(output_path))

    asyncio.run(
        service.classify(
            ImageCandidate(candidate_id="candidate", local_path=image_path, source="local")
        )
    )

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "candidate"
    assert rows[0]["decision"] == "review"


def test_jsonl_decision_overwrite_preserves_old_file_until_first_result(tmp_path):
    image_path = _write_image(tmp_path / "image.jpg", size=(512, 512))
    output_path = tmp_path / "decisions.jsonl"
    output_path.write_text("old-record\n", encoding="utf-8")
    store = JSONLDecisionStore(output_path, append=False)

    assert output_path.read_text(encoding="utf-8") == "old-record\n"
    store.abort()
    assert output_path.read_text(encoding="utf-8") == "old-record\n"

    store = JSONLDecisionStore(output_path, append=False)
    service = ArtworkFilterService(
        load_artwork_filter_config(),
        decision_store=store,
    )
    asyncio.run(
        service.classify(
            ImageCandidate(candidate_id="candidate", local_path=image_path, source="local")
        )
    )

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert [row["candidate_id"] for row in rows] == ["candidate"]


def test_cli_classify_image_json_output(tmp_path, capsys, monkeypatch):
    image_path = _write_image(tmp_path / "image.jpg", size=(512, 512))
    monkeypatch.setattr(
        "sys.argv",
        [
            "artsearch-artwork-filter",
            "classify-image",
            "--path",
            str(image_path),
            "--deterministic-only",
            "--json",
        ],
    )

    main()

    output = json.loads(capsys.readouterr().out)
    assert output["decision"] == "review"
    assert output["route"] == "review"


def _write_image(path: Path, *, size: tuple[int, int]) -> Path:
    Image.new("RGB", size, (128, 64, 32)).save(path)
    return path
