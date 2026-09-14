from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from artsearch.artwork_filter.enums import ContentClass, FilterDecision
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate
from artsearch.discovery.config import (
    DiscoveryConfig,
    load_discovery_config,
    override_discovery_limits,
)
from artsearch.discovery.models import CreatorState, CreatorView, SafetyState
from artsearch.discovery.scoring import profile_safety_state, score_creator


def test_discovery_config_is_versioned_hashed_and_overridable() -> None:
    config = load_discovery_config("configs/discovery.default.toml")
    changed = override_discovery_limits(
        config,
        creator_budget=17,
        media_budget=123,
        max_tasks=45,
        max_depth=1,
        request_budget=99,
    )

    assert len(config.config_hash) == 64
    assert changed.crawl.daily_creator_budget == 17
    assert changed.crawl.daily_media_budget == 123
    assert changed.crawl.max_tasks_per_run == 45
    assert changed.crawl.max_depth == 1
    assert changed.crawl.max_api_requests_per_run == 99
    assert changed.config_hash != config.config_hash
    assert config.crawl.daily_creator_budget == 200
    assert config.crawl.max_expansion_candidates_per_creator == 25
    assert config.crawl.max_outbound_follows_per_creator == 25


def test_positive_creator_weights_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match="sum to 1.0"):
        DiscoveryConfig.model_validate({"scoring": {"art_relevance_weight": 0.1}})


def test_creator_scoring_qualifies_dense_original_art_portfolio() -> None:
    config = DiscoveryConfig()
    profile = CreatorView(
        did="did:plc:artist",
        handle="artist.test",
        display_name="Indie Illustrator",
        description="Character artist, commissions, illustration portfolio",
        followers_count=500,
    )
    candidates = [_candidate(profile.did, index) for index in range(5)]
    results = [_result(item.candidate_id, FilterDecision.ACCEPT, 0.92) for item in candidates]

    scored = score_creator(
        profile,
        candidates,
        results,
        config,
        source_reliability=0.9,
        novelty_score=0.8,
    )

    assert scored.state == CreatorState.QUALIFIED
    assert scored.safety_state == SafetyState.ALLOWED
    assert scored.accepted_media == 5
    assert scored.creator_score >= config.qualification.auto_qualify_score
    assert scored.profile_summary["score_semantics"] == "versioned_decision_score_not_probability"


def test_safety_review_and_missing_source_moderation_prevent_auto_qualification() -> None:
    config = DiscoveryConfig()
    profile = CreatorView(
        did="did:plc:review",
        handle="review.test",
        description="NSFW character artist and illustrator",
    )
    candidates = [
        _candidate(profile.did, index, source_moderation_passed=False) for index in range(5)
    ]
    results = [_result(item.candidate_id, FilterDecision.ACCEPT, 0.99) for item in candidates]

    scored = score_creator(
        profile,
        candidates,
        results,
        config,
        source_reliability=1.0,
        novelty_score=1.0,
    )

    assert profile_safety_state(profile, config) == SafetyState.REVIEW
    assert scored.safety_state == SafetyState.REVIEW
    assert scored.state == CreatorState.NEEDS_REVIEW
    assert scored.profile_summary["source_safety_measured"] is False


def _candidate(
    did: str,
    index: int,
    *,
    source_moderation_passed: bool = True,
) -> ImageCandidate:
    return ImageCandidate(
        candidate_id=f"candidate-{index}",
        author_did=did,
        post_uri=f"at://{did}/app.bsky.feed.post/{index}",
        post_cid=f"cid-{index}",
        image_index=0,
        local_path=Path(f"fixture-{index}.jpg"),
        source="test",
        source_moderation_passed=source_moderation_passed,
    )


def _result(candidate_id: str, decision: FilterDecision, score: float) -> FilterResult:
    return FilterResult(
        candidate_id=candidate_id,
        decision=decision,
        predicted_class=(
            ContentClass.FINISHED_ILLUSTRATION
            if decision == FilterDecision.ACCEPT
            else ContentClass.CASUAL_PHOTO
        ),
        accepted_for_main_corpus=decision == FilterDecision.ACCEPT,
        route="main_art" if decision == FilterDecision.ACCEPT else "rejected",
        final_score=score,
        confidence=score,
        reason_codes=[f"test.{decision.value}"],
        model_version="fake-siglip",
        config_version="test",
        config_hash="f" * 64,
        processed_at=datetime.now(timezone.utc),
        duration_ms=1.0,
    )
