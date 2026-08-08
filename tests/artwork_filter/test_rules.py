from PIL import Image

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.enums import RuleDisposition
from artsearch.artwork_filter.hashing import sha256_bytes
from artsearch.artwork_filter.rules import aggregate_disposition, evaluate_rules
from artsearch.artwork_filter.schemas import ImageCandidate, LoadedImage, RuleHit


def test_minimum_dimensions_force_reject():
    config = load_artwork_filter_config()
    candidate = ImageCandidate(candidate_id="candidate", local_path="image.jpg")
    loaded = _loaded_image(width=120, height=120)

    result = evaluate_rules(candidate, loaded, config)

    assert result.disposition == RuleDisposition.FORCE_REJECT
    assert "reject.minimum_dimensions" in {hit.reason_code for hit in result.hits}


def test_extreme_aspect_ratio_forces_review_for_large_image():
    config = load_artwork_filter_config()
    candidate = ImageCandidate(candidate_id="candidate", local_path="image.jpg")
    loaded = _loaded_image(width=3000, height=300)

    result = evaluate_rules(candidate, loaded, config)

    assert result.disposition == RuleDisposition.FORCE_REVIEW
    assert "review.extreme_aspect_ratio" in {hit.reason_code for hit in result.hits}


def test_repost_forces_review():
    config = load_artwork_filter_config()
    candidate = ImageCandidate(candidate_id="candidate", local_path="image.jpg", is_repost=True)
    loaded = _loaded_image(width=512, height=512)

    result = evaluate_rules(candidate, loaded, config)

    assert result.disposition == RuleDisposition.FORCE_REVIEW
    assert "review.repost_provenance" in {hit.reason_code for hit in result.hits}


def test_rule_precedence_reject_beats_review():
    result = aggregate_disposition(
        [
            RuleHit(
                rule_id="review",
                disposition=RuleDisposition.FORCE_REVIEW,
                reason_code="review.reason",
                message="review",
            ),
            RuleHit(
                rule_id="reject",
                disposition=RuleDisposition.FORCE_REJECT,
                reason_code="reject.reason",
                message="reject",
            ),
        ]
    )

    assert result == RuleDisposition.FORCE_REJECT


def _loaded_image(*, width: int, height: int) -> LoadedImage:
    image = Image.new("RGB", (width, height), (128, 128, 128))
    return LoadedImage(
        candidate_id="candidate",
        rgb_image=image,
        width=width,
        height=height,
        byte_size=width * height,
        sha256=sha256_bytes(b"image"),
    )
