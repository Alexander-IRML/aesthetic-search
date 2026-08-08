from __future__ import annotations

from PIL import ImageStat

from artsearch.artwork_filter.config import ArtworkFilterConfig
from artsearch.artwork_filter.enums import RuleDisposition
from artsearch.artwork_filter.schemas import ImageCandidate, LoadedImage, RuleHit, RuleResult


def evaluate_rules(
    candidate: ImageCandidate,
    loaded_image: LoadedImage,
    config: ArtworkFilterConfig,
) -> RuleResult:
    hits = [
        hit
        for hit in (
            _minimum_dimensions(loaded_image, config),
            _minimum_area(loaded_image, config),
            _extreme_aspect_ratio(loaded_image, config),
            _animation(loaded_image, config),
            _low_variance(loaded_image, config),
            _repost(candidate, config),
            _quote_post(candidate, config),
        )
        if hit is not None
    ]
    return RuleResult(disposition=aggregate_disposition(hits), hits=hits)


def aggregate_disposition(hits: list[RuleHit]) -> RuleDisposition:
    dispositions = {hit.disposition for hit in hits}
    if RuleDisposition.FORCE_REJECT in dispositions:
        return RuleDisposition.FORCE_REJECT
    if RuleDisposition.FORCE_REVIEW in dispositions:
        return RuleDisposition.FORCE_REVIEW
    if RuleDisposition.FORCE_ACCEPT in dispositions:
        return RuleDisposition.FORCE_ACCEPT
    return RuleDisposition.CONTINUE


def _minimum_dimensions(
    loaded_image: LoadedImage,
    config: ArtworkFilterConfig,
) -> RuleHit | None:
    if (
        loaded_image.width >= config.media.min_width
        and loaded_image.height >= config.media.min_height
    ):
        return None
    return RuleHit(
        rule_id="media.minimum_dimensions",
        disposition=RuleDisposition.FORCE_REJECT,
        reason_code="reject.minimum_dimensions",
        message="image is below minimum configured width or height",
        numeric_value=float(min(loaded_image.width, loaded_image.height)),
    )


def _minimum_area(loaded_image: LoadedImage, config: ArtworkFilterConfig) -> RuleHit | None:
    area = loaded_image.width * loaded_image.height
    if area >= config.media.min_area:
        return None
    return RuleHit(
        rule_id="media.minimum_area",
        disposition=RuleDisposition.FORCE_REJECT,
        reason_code="reject.minimum_area",
        message="image area is below minimum configured area",
        numeric_value=float(area),
    )


def _extreme_aspect_ratio(
    loaded_image: LoadedImage,
    config: ArtworkFilterConfig,
) -> RuleHit | None:
    ratio = max(loaded_image.width / loaded_image.height, loaded_image.height / loaded_image.width)
    if ratio <= config.media.max_aspect_ratio:
        return None
    return RuleHit(
        rule_id="media.extreme_aspect_ratio",
        disposition=RuleDisposition.FORCE_REVIEW,
        reason_code="review.extreme_aspect_ratio",
        message="image has an unusually extreme aspect ratio",
        numeric_value=float(ratio),
    )


def _animation(loaded_image: LoadedImage, config: ArtworkFilterConfig) -> RuleHit | None:
    if not loaded_image.is_animated or config.media.allow_animated:
        return None
    return RuleHit(
        rule_id="media.animation",
        disposition=RuleDisposition.FORCE_REJECT,
        reason_code="reject.unsupported_animation",
        message="animated images are not enabled for this filter",
    )


def _low_variance(loaded_image: LoadedImage, config: ArtworkFilterConfig) -> RuleHit | None:
    if not config.media.review_low_variance:
        return None
    extrema = ImageStat.Stat(loaded_image.rgb_image).stddev
    value = max(extrema)
    if value >= config.media.low_variance_threshold:
        return None
    return RuleHit(
        rule_id="media.low_variance",
        disposition=RuleDisposition.FORCE_REVIEW,
        reason_code="review.low_variance",
        message="image has very low pixel variance",
        numeric_value=float(value),
    )


def _repost(candidate: ImageCandidate, config: ArtworkFilterConfig) -> RuleHit | None:
    if not candidate.is_repost or not config.reposts.force_review_reposts:
        return None
    return RuleHit(
        rule_id="provenance.repost",
        disposition=RuleDisposition.FORCE_REVIEW,
        reason_code="review.repost_provenance",
        message="reposts require provenance review before corpus inclusion",
    )


def _quote_post(candidate: ImageCandidate, config: ArtworkFilterConfig) -> RuleHit | None:
    if not candidate.is_quote_post or not config.reposts.force_review_reposts:
        return None
    return RuleHit(
        rule_id="provenance.quote_post",
        disposition=RuleDisposition.FORCE_REVIEW,
        reason_code="review.quote_post_provenance",
        message="quote posts require provenance review before corpus inclusion",
    )
