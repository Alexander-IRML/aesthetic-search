from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from artsearch.artwork_filter.enums import ContentClass, FilterDecision
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate
from artsearch.discovery.config import DiscoveryConfig
from artsearch.discovery.fakes import (
    FakeDiscoveryOracle,
    FakeHydrator,
    FakeMediaProcessor,
    InMemoryDiscoveryStore,
)
from artsearch.discovery.models import (
    CreatorProfile,
    CreatorState,
    CreatorView,
    DiscoveryRequest,
    DiscoverySourceType,
    DiscoverySuggestion,
    ExpansionAction,
    GraphEdgeType,
    GraphEntityType,
    MediaBatchOutcome,
    SafetyState,
)
from artsearch.discovery.pipeline import DiscoveryCoordinator, InitialDiscoverySource


def test_same_did_keeps_multiple_observations_but_samples_once() -> None:
    profile = _profile("one")
    candidate = _candidate(profile.did, "one")
    result = _result(candidate.candidate_id)
    store = InMemoryDiscoveryStore()
    oracle = FakeDiscoveryOracle(
        [
            _suggestion(profile, "starter-pack:a"),
            _suggestion(profile, "post-search:b"),
            _suggestion(profile, "starter-pack:a"),
        ]
    )
    coordinator = DiscoveryCoordinator(
        _config(max_depth=0),
        store,
        FakeHydrator([profile], {profile.did: [candidate]}),
        FakeMediaProcessor(
            {profile.did: MediaBatchOutcome((candidate,), (result,), {"accepted": 1})}
        ),
    )

    summary = asyncio.run(
        coordinator.run(
            "dedupe",
            [InitialDiscoverySource(oracle, DiscoveryRequest(limit=10), "fake")],
        )
    )

    assert summary.unique_creators == 1
    assert summary.observations == 2
    assert summary.creators_sampled == 1
    assert len(store.creators) == 1
    assert len(store.observations) == 2
    assert store.profiles[profile.did].state == CreatorState.QUALIFIED


def test_qualified_creator_expands_outbound_graph_only_to_max_depth() -> None:
    first, second = _profile("one"), _profile("two")
    candidates = {
        profile.did: [_candidate(profile.did, profile.handle)] for profile in (first, second)
    }
    outcomes = {
        did: MediaBatchOutcome(
            tuple(items),
            tuple(_result(item.candidate_id) for item in items),
            {"candidates": 1, "accepted": 1, "imported": 1},
        )
        for did, items in candidates.items()
    }
    store = InMemoryDiscoveryStore()
    coordinator = DiscoveryCoordinator(
        _config(max_depth=1),
        store,
        FakeHydrator([first, second], candidates),
        FakeMediaProcessor(outcomes),
        outbound_oracle=FakeDiscoveryOracle(
            [
                DiscoverySuggestion(
                    actor_ref=second.did,
                    profile=second,
                    source_type=DiscoverySourceType.OUTBOUND_FOLLOW,
                    source_ref=first.did,
                    graph_parent_type=GraphEntityType.CREATOR,
                    graph_parent_ref=first.did,
                    graph_edge_type=GraphEdgeType.FOLLOWS,
                    depth=1,
                    confidence=0.8,
                )
            ]
        ),
    )

    summary = asyncio.run(
        coordinator.run(
            "expansion",
            [
                InitialDiscoverySource(
                    FakeDiscoveryOracle([_suggestion(first, "manual")]),
                    DiscoveryRequest(limit=1),
                    "manual",
                )
            ],
        )
    )

    assert summary.unique_creators == 2
    assert summary.creators_sampled == 2
    assert summary.creators_qualified == 2
    assert summary.accepted == 2
    assert all(task.depth <= 1 for task in store.tasks.values())
    assert all(task.status.value == "completed" for task in store.tasks.values())


def test_creator_expansion_is_capped_at_25_unique_accounts_across_sources() -> None:
    seed = _profile("seed")
    outbound = [_profile(f"outbound-{index}") for index in range(20)]
    suggested = [_profile(f"suggested-{index}") for index in range(20)]
    discovered = [*outbound, *suggested]
    media = {profile.did: [_candidate(profile.did, profile.handle)] for profile in discovered}
    config = _config(max_depth=1)
    config.sources.suggested_follows = True
    config.crawl.daily_creator_budget = 50
    config.crawl.daily_media_budget = 50
    config.crawl.max_tasks_per_run = 100
    store = InMemoryDiscoveryStore()
    coordinator = DiscoveryCoordinator(
        config,
        store,
        FakeHydrator([seed, *discovered], media),
        _DecisionMediaProcessor({profile.did: FilterDecision.ACCEPT for profile in discovered}),
        outbound_oracle=FakeDiscoveryOracle(
            [
                _expansion_suggestion(profile, seed, DiscoverySourceType.OUTBOUND_FOLLOW)
                for profile in outbound
            ]
        ),
        suggested_oracle=FakeDiscoveryOracle(
            [
                _expansion_suggestion(profile, seed, DiscoverySourceType.SUGGESTED_FOLLOW)
                for profile in suggested
            ]
        ),
    )

    summary = asyncio.run(
        coordinator.run(
            "bounded-fanout",
            [
                InitialDiscoverySource(
                    FakeDiscoveryOracle([_suggestion(seed, "existing-corpus")]),
                    DiscoveryRequest(limit=1),
                    "existing-corpus",
                    expand_without_sampling=True,
                )
            ],
        )
    )

    assert len(store.expansion_child_dids(seed.did)) == 25
    assert summary.expansion_candidates_added == 25
    assert summary.expansion_nodes_at_limit == 1
    assert summary.creators_sampled == 25


def test_duplicate_expansion_account_ids_are_observable_but_sampled_once() -> None:
    seed = _profile("duplicate-seed")
    shared = _profile("shared-child")
    outbound_only = _profile("outbound-only")
    suggested_only = _profile("suggested-only")
    discovered = [shared, outbound_only, suggested_only]
    media = {profile.did: [_candidate(profile.did, profile.handle)] for profile in discovered}
    config = _config(max_depth=1)
    config.sources.suggested_follows = True
    store = InMemoryDiscoveryStore()
    coordinator = DiscoveryCoordinator(
        config,
        store,
        FakeHydrator([seed, *discovered], media),
        _DecisionMediaProcessor({profile.did: FilterDecision.ACCEPT for profile in discovered}),
        outbound_oracle=FakeDiscoveryOracle(
            [
                _expansion_suggestion(shared, seed, DiscoverySourceType.OUTBOUND_FOLLOW),
                _expansion_suggestion(
                    outbound_only,
                    seed,
                    DiscoverySourceType.OUTBOUND_FOLLOW,
                ),
            ]
        ),
        suggested_oracle=FakeDiscoveryOracle(
            [
                _expansion_suggestion(shared, seed, DiscoverySourceType.SUGGESTED_FOLLOW),
                _expansion_suggestion(
                    suggested_only,
                    seed,
                    DiscoverySourceType.SUGGESTED_FOLLOW,
                ),
            ]
        ),
    )

    summary = asyncio.run(
        coordinator.run(
            "duplicate-watch",
            [
                InitialDiscoverySource(
                    FakeDiscoveryOracle([_suggestion(seed, "existing-corpus")]),
                    DiscoveryRequest(limit=1),
                    "existing-corpus",
                    expand_without_sampling=True,
                )
            ],
        )
    )

    assert store.expansion_child_dids(seed.did) == {
        shared.did,
        outbound_only.did,
        suggested_only.did,
    }
    assert summary.duplicate_creator_ids == 1
    assert summary.duplicate_expansion_account_ids == 1
    assert summary.creators_sampled == 3
    sampled_creators = [creator_did for creator_did, _ in store.media_results]
    assert sampled_creators.count(shared.did) == 1


def test_discovered_neighbor_must_pass_small_media_qualification_before_expanding() -> None:
    seed = _profile("qualification-seed")
    non_artist = CreatorView(
        did="did:plc:non-artist-neighbor",
        handle="non-artist-neighbor.test",
        display_name="Personal account",
        description="daily life and occasional photos",
        followers_count=500,
    )
    seed_candidate = _candidate(seed.did, seed.handle)
    neighbor_candidate = _candidate(non_artist.did, non_artist.handle)
    store = InMemoryDiscoveryStore()
    coordinator = DiscoveryCoordinator(
        _config(max_depth=2),
        store,
        FakeHydrator(
            [seed, non_artist],
            {
                seed.did: [seed_candidate],
                non_artist.did: [neighbor_candidate],
            },
        ),
        _DecisionMediaProcessor(
            {
                seed.did: FilterDecision.ACCEPT,
                non_artist.did: FilterDecision.REJECT,
            }
        ),
        outbound_oracle=FakeDiscoveryOracle(
            [
                _expansion_suggestion(
                    non_artist,
                    seed,
                    DiscoverySourceType.OUTBOUND_FOLLOW,
                )
            ]
        ),
    )

    summary = asyncio.run(
        coordinator.run(
            "qualification-gate",
            [
                InitialDiscoverySource(
                    FakeDiscoveryOracle([_suggestion(seed, "manual")]),
                    DiscoveryRequest(limit=1),
                    "manual",
                )
            ],
        )
    )

    assert summary.creators_qualified == 1
    assert summary.creators_review == 1
    assert store.profiles[non_artist.did].state == CreatorState.NEEDS_REVIEW
    assert not any(
        task.payload.get("did") == non_artist.did
        and task.action
        in {
            ExpansionAction.EXPAND_OUTBOUND_FOLLOWS,
            ExpansionAction.EXPAND_SUGGESTED_FOLLOWS,
        }
        for task in store.tasks.values()
    )


def test_global_creator_budget_bounds_parallel_source_yield() -> None:
    profiles = [_profile(str(index)) for index in range(5)]
    media = {profile.did: [_candidate(profile.did, profile.handle)] for profile in profiles}
    outcomes = {
        did: MediaBatchOutcome(tuple(items), tuple(_result(item.candidate_id) for item in items))
        for did, items in media.items()
    }
    store = InMemoryDiscoveryStore()
    config = _config(max_depth=0)
    config.crawl.daily_creator_budget = 2
    coordinator = DiscoveryCoordinator(
        config,
        store,
        FakeHydrator(profiles, media),
        FakeMediaProcessor(outcomes),
    )
    sources = [
        InitialDiscoverySource(
            FakeDiscoveryOracle([_suggestion(profile, f"source:{profile.did}")]),
            DiscoveryRequest(limit=1),
            profile.did,
        )
        for profile in profiles
    ]

    summary = asyncio.run(coordinator.run("budget", sources))

    assert summary.unique_creators == 2
    assert summary.creators_sampled == 2
    assert summary.budget_reached == 1


def test_progressive_sampling_deepens_only_preliminarily_qualified_creator() -> None:
    strong, weak = _profile("strong"), _profile("weak")
    media = {
        strong.did: [_candidate(strong.did, f"strong-{index}") for index in range(3)],
        weak.did: [_candidate(weak.did, f"weak-{index}") for index in range(3)],
    }
    processor = _DecisionMediaProcessor(
        {
            strong.did: FilterDecision.ACCEPT,
            weak.did: FilterDecision.REJECT,
        }
    )
    config = _config(max_depth=0)
    config.crawl.deep_media_sample = 3
    store = InMemoryDiscoveryStore()
    coordinator = DiscoveryCoordinator(
        config,
        store,
        FakeHydrator([strong, weak], media),
        processor,
    )

    summary = asyncio.run(
        coordinator.run(
            "progressive",
            [
                InitialDiscoverySource(
                    FakeDiscoveryOracle(
                        [_suggestion(strong, "manual"), _suggestion(weak, "manual")]
                    ),
                    DiscoveryRequest(limit=2),
                    "manual",
                )
            ],
        )
    )

    assert processor.calls.count(("candidate-strong-0",)) == 1
    assert ("candidate-strong-1", "candidate-strong-2") in processor.calls
    assert processor.calls.count(("candidate-weak-0",)) == 1
    assert not any("candidate-weak-1" in call for call in processor.calls)
    assert summary.creators_deepened == 1
    assert summary.accepted == 3
    assert summary.rejected == 1


def test_fresh_qualified_creator_skips_resampling_but_can_expand() -> None:
    profile = _profile("fresh")
    store = InMemoryDiscoveryStore()
    store.creators[profile.did] = profile
    store.creator_states[profile.did] = CreatorState.QUALIFIED.value
    store.sampled_at[profile.did] = datetime.now(timezone.utc)
    store.profiles[profile.did] = _creator_profile(profile, score_version="creator-score-v1")
    processor = _DecisionMediaProcessor({profile.did: FilterDecision.ACCEPT})
    coordinator = DiscoveryCoordinator(
        _config(max_depth=1),
        store,
        FakeHydrator([profile], {profile.did: [_candidate(profile.did, "fresh")]}),
        processor,
        outbound_oracle=FakeDiscoveryOracle([]),
    )

    summary = asyncio.run(
        coordinator.run(
            "fresh",
            [
                InitialDiscoverySource(
                    FakeDiscoveryOracle([_suggestion(profile, "manual")]),
                    DiscoveryRequest(limit=1),
                    "manual",
                )
            ],
        )
    )

    assert processor.calls == []
    assert summary.creators_sampled == 0
    assert any(task.action.value == "expand_outbound_follows" for task in store.tasks.values())


def test_existing_corpus_seed_expands_without_reprocessing_its_media() -> None:
    profile = _profile("corpus-seed")
    store = InMemoryDiscoveryStore()
    processor = _DecisionMediaProcessor({})
    coordinator = DiscoveryCoordinator(
        _config(max_depth=1),
        store,
        FakeHydrator([profile]),
        processor,
        outbound_oracle=FakeDiscoveryOracle([]),
    )

    summary = asyncio.run(
        coordinator.run(
            "trusted-seed",
            [
                InitialDiscoverySource(
                    FakeDiscoveryOracle([_suggestion(profile, "existing-corpus")]),
                    DiscoveryRequest(limit=1),
                    "existing-corpus",
                    expand_without_sampling=True,
                )
            ],
        )
    )

    assert processor.calls == []
    assert summary.creators_sampled == 0
    assert any(
        task.action == ExpansionAction.EXPAND_OUTBOUND_FOLLOWS for task in store.tasks.values()
    )
    assert all(task.status.value == "completed" for task in store.tasks.values())


def test_score_version_change_forces_creator_resampling() -> None:
    profile = _profile("stale-model")
    candidate = _candidate(profile.did, "stale-model")
    store = InMemoryDiscoveryStore()
    store.creators[profile.did] = profile
    store.creator_states[profile.did] = CreatorState.QUALIFIED.value
    store.sampled_at[profile.did] = datetime.now(timezone.utc)
    store.profiles[profile.did] = _creator_profile(profile, score_version="creator-score-v0")
    processor = _DecisionMediaProcessor({profile.did: FilterDecision.ACCEPT})
    coordinator = DiscoveryCoordinator(
        _config(max_depth=0),
        store,
        FakeHydrator([profile], {profile.did: [candidate]}),
        processor,
    )

    summary = asyncio.run(
        coordinator.run(
            "score-version",
            [
                InitialDiscoverySource(
                    FakeDiscoveryOracle([_suggestion(profile, "manual")]),
                    DiscoveryRequest(limit=1),
                    "manual",
                )
            ],
        )
    )

    assert processor.calls == [(candidate.candidate_id,)]
    assert summary.creators_sampled == 1


def test_new_campaign_recovers_due_frontier_work_from_previous_campaign() -> None:
    profile = _profile("recovered")
    candidate = _candidate(profile.did, "recovered")
    config = _config(max_depth=0)
    store = InMemoryDiscoveryStore()
    previous_campaign = store.create_campaign("interrupted", config)
    store.creators[profile.did] = profile
    store.creator_states[profile.did] = CreatorState.HYDRATED.value
    assert store.enqueue_task(
        previous_campaign,
        entity_id=f"creator:{profile.did}",
        action=ExpansionAction.SAMPLE_CREATOR_MEDIA,
        depth=0,
        priority=0.9,
        budget_class="media",
        max_attempts=3,
        payload={"did": profile.did, "source_reliability": 1.0},
    )
    processor = _DecisionMediaProcessor({profile.did: FilterDecision.ACCEPT})
    coordinator = DiscoveryCoordinator(
        config,
        store,
        FakeHydrator([profile], {profile.did: [candidate]}),
        processor,
    )

    summary = asyncio.run(coordinator.run("recovery", []))

    recovered = [task for task in store.tasks.values() if task.campaign_id == previous_campaign]
    assert processor.calls == [(candidate.candidate_id,)]
    assert summary.tasks_completed == 1
    assert recovered[0].status.value == "completed"


def test_new_campaign_does_not_duplicate_active_frontier_work() -> None:
    config = _config(max_depth=1)
    store = InMemoryDiscoveryStore()
    previous = store.create_campaign("previous", config)
    current = store.create_campaign("current", config)
    arguments = {
        "entity_id": "creator:shared",
        "action": ExpansionAction.SAMPLE_CREATOR_MEDIA,
        "depth": 0,
        "priority": 0.9,
        "budget_class": "media",
        "max_attempts": 3,
        "payload": {"did": "did:plc:shared"},
    }

    assert store.enqueue_task(previous, **arguments)
    assert not store.enqueue_task(current, **arguments)


def test_blocked_creator_is_not_expanded_from_an_already_queued_task() -> None:
    profile = _profile("blocked-parent")
    neighbor = _profile("neighbor")
    config = _config(max_depth=1)
    store = InMemoryDiscoveryStore()
    previous = store.create_campaign("previous", config)
    assert store.enqueue_task(
        previous,
        entity_id=f"creator:{profile.did}",
        action=ExpansionAction.EXPAND_OUTBOUND_FOLLOWS,
        depth=0,
        priority=0.9,
        budget_class="graph",
        max_attempts=3,
        payload={"did": profile.did},
    )
    store.add_exclusion(
        subject_type="creator",
        subject_key=profile.did,
        scope="discovery",
        reason_code="test.blocked",
        source="test",
    )
    coordinator = DiscoveryCoordinator(
        config,
        store,
        FakeHydrator([profile, neighbor]),
        _DecisionMediaProcessor({}),
        outbound_oracle=FakeDiscoveryOracle([_suggestion(neighbor, profile.did)]),
    )

    summary = asyncio.run(coordinator.run("blocked-recovery", []))

    assert summary.tasks_completed == 1
    assert summary.observations == 0
    assert summary.unique_creators == 0


def test_frontier_batch_reserves_configured_low_priority_exploration_slice() -> None:
    config = _config(max_depth=0)
    config.crawl.exploration_fraction = 0.25
    store = InMemoryDiscoveryStore()
    campaign = store.create_campaign("exploration", config)
    for index, priority in enumerate((0.9, 0.8, 0.7, 0.6, 0.1)):
        assert store.enqueue_task(
            campaign,
            entity_id=f"creator:{index}",
            action=ExpansionAction.SAMPLE_CREATOR_MEDIA,
            depth=0,
            priority=priority,
            budget_class="media",
            max_attempts=3,
            payload={"did": f"did:plc:{index}"},
        )
    coordinator = DiscoveryCoordinator(
        config,
        store,
        FakeHydrator([]),
        _DecisionMediaProcessor({}),
    )

    claimed = coordinator._claim_frontier_batch(4)

    assert {task.priority for task in claimed} == {0.9, 0.8, 0.7, 0.1}
    assert all(task.status.value == "leased" for task in claimed)


def _config(*, max_depth: int) -> DiscoveryConfig:
    return DiscoveryConfig.model_validate(
        {
            "sources": {
                "manual_seeds": True,
                "actor_search": False,
                "post_search": False,
                "starter_pack_search": False,
                "outbound_follows": True,
                "suggested_follows": False,
            },
            "crawl": {
                "max_depth": max_depth,
                "initial_media_sample": 1,
                "deep_media_sample": 1,
                "daily_creator_budget": 20,
                "daily_media_budget": 20,
                "max_tasks_per_run": 20,
            },
            "qualification": {
                "minimum_evaluated_media": 1,
                "auto_qualify_score": 0.55,
                "reject_below_score": 0.2,
                "minimum_art_rate": 0.4,
                "minimum_accept_rate": 0.2,
            },
        }
    )


def _profile(name: str) -> CreatorView:
    return CreatorView(
        did=f"did:plc:{name}",
        handle=f"{name}.test",
        display_name=f"Artist {name}",
        description="independent character artist illustration commissions portfolio",
        followers_count=500,
    )


def _suggestion(profile: CreatorView, source_ref: str) -> DiscoverySuggestion:
    return DiscoverySuggestion(
        actor_ref=profile.did,
        profile=profile,
        source_type=DiscoverySourceType.MANUAL_SEED,
        source_ref=source_ref,
        graph_parent_type=GraphEntityType.SOURCE,
        graph_parent_ref=source_ref,
        graph_edge_type=GraphEdgeType.SEARCH_HIT,
        confidence=1.0,
    )


def _expansion_suggestion(
    profile: CreatorView,
    parent: CreatorView,
    source_type: DiscoverySourceType,
) -> DiscoverySuggestion:
    if source_type == DiscoverySourceType.OUTBOUND_FOLLOW:
        edge_type = GraphEdgeType.FOLLOWS
    elif source_type == DiscoverySourceType.SUGGESTED_FOLLOW:
        edge_type = GraphEdgeType.SUGGESTED_FOLLOW
    else:
        raise ValueError(f"unsupported expansion source: {source_type.value}")
    return DiscoverySuggestion(
        actor_ref=profile.did,
        profile=profile,
        source_type=source_type,
        source_ref=parent.did,
        graph_parent_type=GraphEntityType.CREATOR,
        graph_parent_ref=parent.did,
        graph_edge_type=edge_type,
        depth=1,
        confidence=0.75,
    )


def _candidate(did: str, name: str) -> ImageCandidate:
    return ImageCandidate(
        candidate_id=f"candidate-{name}",
        author_did=did,
        post_uri=f"at://{did}/app.bsky.feed.post/{name}",
        post_cid=f"cid-{name}",
        image_index=0,
        local_path=Path(f"{name}.jpg"),
        source="test",
        source_moderation_passed=True,
    )


def _result(candidate_id: str) -> FilterResult:
    return FilterResult(
        candidate_id=candidate_id,
        decision=FilterDecision.ACCEPT,
        predicted_class=ContentClass.FINISHED_ILLUSTRATION,
        accepted_for_main_corpus=True,
        route="main_art",
        final_score=0.95,
        confidence=0.95,
        reason_codes=["accept.test"],
        model_version="fake-siglip",
        config_version="test",
        config_hash="a" * 64,
        processed_at=datetime.now(timezone.utc),
        duration_ms=1.0,
    )


def _creator_profile(profile: CreatorView, *, score_version: str) -> CreatorProfile:
    return CreatorProfile(
        did=profile.did,
        state=CreatorState.QUALIFIED,
        safety_state=SafetyState.ALLOWED,
        creator_score=0.9,
        expansion_score=0.8,
        score_version=score_version,
        score_components={"art_relevance": 1.0},
        sampled_media=1,
        accepted_media=1,
        review_media=0,
        rejected_media=0,
        error_media=0,
    )


class _DecisionMediaProcessor:
    def __init__(self, decisions: dict[str, FilterDecision]) -> None:
        self.decisions = decisions
        self.calls: list[tuple[str, ...]] = []

    async def process(
        self,
        candidates: Sequence[ImageCandidate],
    ) -> MediaBatchOutcome:
        self.calls.append(tuple(item.candidate_id for item in candidates))
        decision = self.decisions[candidates[0].author_did or ""]
        results = tuple(_result_for_decision(item.candidate_id, decision) for item in candidates)
        return MediaBatchOutcome(
            candidates=tuple(candidates),
            results=results,
            pipeline_summary={
                "candidates": len(candidates),
                "accepted": sum(item.decision == FilterDecision.ACCEPT for item in results),
                "review": sum(item.decision == FilterDecision.REVIEW for item in results),
                "rejected": sum(item.decision == FilterDecision.REJECT for item in results),
                "classification_errors": sum(
                    item.decision == FilterDecision.ERROR for item in results
                ),
            },
        )


def _result_for_decision(
    candidate_id: str,
    decision: FilterDecision,
) -> FilterResult:
    result = _result(candidate_id)
    if decision == FilterDecision.ACCEPT:
        return result
    return result.model_copy(
        update={
            "decision": decision,
            "predicted_class": ContentClass.CASUAL_PHOTO,
            "accepted_for_main_corpus": False,
            "route": "rejected" if decision == FilterDecision.REJECT else decision.value,
            "final_score": 0.05,
            "confidence": 0.95,
            "reason_codes": [f"test.{decision.value}"],
        }
    )
