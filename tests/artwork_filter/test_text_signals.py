from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.artwork_filter.text_signals import normalize_text, score_text


def test_empty_text_is_neutral():
    scores = score_text(ImageCandidate(candidate_id="candidate", local_path="image.jpg"))

    assert scores.positive_score == 0.0
    assert scores.negative_score == 0.0
    assert scores.net_score == 0.0


def test_matches_phrases_before_tokens_and_reports_terms():
    candidate = ImageCandidate(
        candidate_id="candidate",
        local_path="image.jpg",
        post_text="Finished reference sheet commission prices",
        alt_text="character design",
    )

    scores = score_text(candidate)

    assert "reference sheet" in scores.matched_positive_terms
    assert "character design" in scores.matched_positive_terms
    assert "commission prices" in scores.matched_patterns
    assert scores.positive_score > 0.0
    assert scores.negative_score > 0.0


def test_unicode_and_whitespace_are_normalized():
    assert normalize_text("  Work\u00a0in   Progress  ") == "work in progress"
