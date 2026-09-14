from __future__ import annotations

from collections.abc import Sequence
import math
import re
import unicodedata

from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate
from artsearch.discovery.config import DiscoveryConfig
from artsearch.discovery.models import CreatorProfile, CreatorState, CreatorView, SafetyState


_CREATOR_TERMS = (
    "artist",
    "artwork",
    "illustrator",
    "illustration",
    "animator",
    "comic",
    "commissions",
    "character design",
    "concept art",
    "pixel art",
    "portfolio",
)


def profile_safety_state(profile: CreatorView, config: DiscoveryConfig) -> SafetyState:
    blocked_labels = set(config.safety.block_profile_labels)
    if blocked_labels.intersection(label.casefold() for label in profile.labels):
        return SafetyState.BLOCKED
    normalized = unicodedata.normalize("NFKC", profile.description).casefold()
    if any(_contains_term(normalized, term) for term in config.safety.review_profile_terms):
        return SafetyState.REVIEW
    return SafetyState.ALLOWED


def score_creator(
    profile: CreatorView,
    candidates: Sequence[ImageCandidate],
    results: Sequence[FilterResult],
    config: DiscoveryConfig,
    *,
    source_reliability: float,
    novelty_score: float = 0.5,
) -> CreatorProfile:
    """Aggregate auditable work-level decisions without claiming authorship probability."""

    if len(candidates) != len(results):
        raise ValueError("candidates and results must have the same length")
    accepted = sum(result.decision == FilterDecision.ACCEPT for result in results)
    review = sum(result.decision == FilterDecision.REVIEW for result in results)
    rejected = sum(result.decision == FilterDecision.REJECT for result in results)
    errors = sum(result.decision == FilterDecision.ERROR for result in results)
    evaluated = max(0, len(results) - errors)
    denominator = max(1, evaluated)

    art_relevance = _clamp((accepted + 0.5 * review) / denominator)
    portfolio_density = _clamp((accepted + review) / denominator)
    accept_rate = _clamp(accepted / denominator)
    useful_scores = [
        result.final_score
        for result in results
        if result.decision in {FilterDecision.ACCEPT, FilterDecision.REVIEW}
    ]
    visual_quality = _clamp(sum(useful_scores) / len(useful_scores)) if useful_scores else 0.0
    original_post_rate = _clamp(
        sum(not item.is_repost and not item.is_quote_post for item in candidates)
        / max(1, len(candidates))
    )
    repost_ratio = 1.0 - original_post_rate
    creator_identity = creator_identity_signal(profile)
    activity = _clamp(evaluated / max(1, config.crawl.initial_media_sample))
    source_reliability = _clamp(source_reliability)
    novelty_score = _clamp(novelty_score)
    popularity_penalty, long_tail_bonus = _popularity_adjustments(profile.followers_count)

    weights = config.scoring
    base_score = (
        weights.art_relevance_weight * art_relevance
        + weights.portfolio_density_weight * portfolio_density
        + weights.visual_quality_weight * visual_quality
        + weights.creator_identity_weight * creator_identity
        + weights.source_reliability_weight * source_reliability
        + weights.novelty_weight * novelty_score
        + weights.activity_weight * activity
    )
    creator_score = _clamp(
        base_score
        - weights.repost_penalty_weight * repost_ratio
        - weights.popularity_penalty_weight * popularity_penalty
        + weights.long_tail_bonus_weight * long_tail_bonus
    )
    expansion_score = _clamp(0.75 * creator_score + 0.25 * novelty_score)
    safety = profile_safety_state(profile, config)
    source_safety_measured = bool(candidates) and all(
        candidate.source_moderation_passed for candidate in candidates
    )
    if config.safety.require_source_moderation and not source_safety_measured:
        safety = SafetyState.REVIEW if safety != SafetyState.BLOCKED else safety

    thresholds = config.qualification
    if safety == SafetyState.BLOCKED:
        state = CreatorState.BLOCKED
    elif evaluated < thresholds.minimum_evaluated_media:
        state = CreatorState.NEEDS_REVIEW
    elif (
        safety == SafetyState.ALLOWED
        and creator_score >= thresholds.auto_qualify_score
        and art_relevance >= thresholds.minimum_art_rate
        and accept_rate >= thresholds.minimum_accept_rate
    ):
        state = CreatorState.QUALIFIED
    elif creator_score <= thresholds.reject_below_score:
        state = CreatorState.REJECTED
    else:
        state = CreatorState.NEEDS_REVIEW

    components = {
        "art_relevance": art_relevance,
        "portfolio_density": portfolio_density,
        "accept_rate": accept_rate,
        "visual_quality": visual_quality,
        "creator_identity": creator_identity,
        "source_reliability": source_reliability,
        "novelty": novelty_score,
        "activity": activity,
        "original_post_rate": original_post_rate,
        "repost_ratio": repost_ratio,
        "popularity_penalty": popularity_penalty,
        "long_tail_bonus": long_tail_bonus,
    }
    return CreatorProfile(
        did=profile.did,
        state=state,
        safety_state=safety,
        creator_score=creator_score,
        expansion_score=expansion_score,
        score_version=weights.version,
        score_components=components,
        sampled_media=len(results),
        accepted_media=accepted,
        review_media=review,
        rejected_media=rejected,
        error_media=errors,
        profile_summary={
            "handle": profile.handle,
            "display_name": profile.display_name,
            "source_safety_measured": source_safety_measured,
            "score_semantics": "versioned_decision_score_not_probability",
        },
    )


def creator_identity_signal(profile: CreatorView) -> float:
    text = unicodedata.normalize(
        "NFKC",
        f"{profile.display_name}\n{profile.description}",
    ).casefold()
    matches = sum(_contains_term(text, term) for term in _CREATOR_TERMS)
    return _clamp(matches / 3.0)


def _popularity_adjustments(followers: int | None) -> tuple[float, float]:
    if followers is None or followers < 0:
        return 0.0, 0.0
    if followers < 1_000:
        return 0.0, 1.0
    if followers < 10_000:
        return 0.1, 0.6
    if followers < 100_000:
        return 0.4, 0.2
    return min(1.0, math.log10(max(followers, 100_000)) / 7.0), 0.0


def _contains_term(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", text) is not None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
