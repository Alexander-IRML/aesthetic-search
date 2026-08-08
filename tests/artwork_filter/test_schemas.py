from pathlib import Path

import pytest
from pydantic import ValidationError

from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate


def test_image_candidate_requires_an_image_source():
    with pytest.raises(ValidationError):
        ImageCandidate(candidate_id="candidate")


def test_image_candidate_validates_index_and_candidate_id():
    with pytest.raises(ValidationError):
        ImageCandidate(candidate_id="candidate", local_path=Path("image.jpg"), image_index=-1)

    with pytest.raises(ValidationError):
        ImageCandidate(candidate_id="https://example.com/image.jpg", local_path=Path("image.jpg"))


def test_image_candidate_allows_did_without_handle():
    candidate = ImageCandidate(
        candidate_id="candidate",
        author_did="did:plc:example",
        local_path=Path("image.jpg"),
        post_text=None,
        alt_text=None,
    )

    assert candidate.author_handle is None
    assert candidate.post_text == ""
    assert candidate.alt_text == ""


def test_filter_result_serializes_enums_and_timestamps():
    result = FilterResult(
        candidate_id="candidate",
        decision=FilterDecision.REVIEW,
        predicted_class="unknown",
        accepted_for_main_corpus=False,
        route="review",
        final_score=0.5,
        confidence=0.0,
        reason_codes=["review.no_visual_model"],
        model_version="model",
        config_version="1.0.0",
        processed_at="2026-07-16T00:00:00Z",
        duration_ms=1.0,
        error_type=None,
        error_message=None,
    )

    assert result.model_dump(mode="json")["decision"] == "review"
    assert result.model_dump(mode="json")["predicted_class"] == "unknown"
