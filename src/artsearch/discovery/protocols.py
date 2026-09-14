from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol
import uuid

from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate
from artsearch.discovery.models import (
    CreatorView,
    CampaignState,
    CreatorProfile,
    CreatorSamplingStatus,
    DiscoveryRunSummary,
    DiscoveryRequest,
    DiscoverySuggestion,
    MediaBatchOutcome,
    NetworkEvent,
    ExpansionAction,
    FrontierStatus,
    FrontierTask,
    SafetyState,
)
from artsearch.discovery.config import DiscoveryConfig


class DiscoveryOracle(Protocol):
    async def suggest(
        self,
        request: DiscoveryRequest,
    ) -> AsyncIterator[DiscoverySuggestion]: ...


class NetworkStream(Protocol):
    async def events(self) -> AsyncIterator[NetworkEvent]: ...


class Hydrator(Protocol):
    async def creator(self, actor: str) -> CreatorView: ...

    async def media_candidates(
        self,
        did: str,
        *,
        limit: int,
    ) -> list[ImageCandidate]: ...


class MediaProcessor(Protocol):
    async def process(self, candidates: Sequence[ImageCandidate]) -> MediaBatchOutcome: ...


class DiscoveryStore(Protocol):
    def create_campaign(self, name: str, config: DiscoveryConfig) -> uuid.UUID: ...

    def finish_campaign(
        self,
        campaign_id: uuid.UUID,
        state: CampaignState,
        summary: DiscoveryRunSummary,
    ) -> None: ...

    def record_suggestion(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        suggestion: DiscoverySuggestion,
        profile: CreatorView,
        *,
        safety_state: SafetyState,
    ) -> bool: ...

    def hydrate_creator(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        profile: CreatorView,
        *,
        safety_state: SafetyState,
    ) -> None: ...

    def creator_sampling_status(self, did: str) -> CreatorSamplingStatus | None: ...

    def expansion_child_dids(self, creator_did: str) -> set[str]: ...

    def record_media_results(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        creator_did: str,
        candidates: Sequence[ImageCandidate],
        results: Sequence[FilterResult],
    ) -> None: ...

    def record_creator_profile(
        self,
        campaign_id: uuid.UUID,
        run_id: str,
        profile: CreatorProfile,
        *,
        config_hash: str,
    ) -> None: ...

    def enqueue_task(
        self,
        campaign_id: uuid.UUID,
        *,
        entity_id: str,
        action: ExpansionAction,
        depth: int,
        priority: float,
        budget_class: str,
        max_attempts: int,
        parent_task_id: uuid.UUID | None = None,
        payload: dict[str, object] | None = None,
    ) -> bool: ...

    def claim_tasks(
        self,
        worker_id: str,
        *,
        limit: int,
        lease_seconds: int,
        campaign_id: uuid.UUID | None = None,
        prefer_high_priority: bool = True,
    ) -> list[FrontierTask]: ...

    def complete_task(self, task_id: uuid.UUID, worker_id: str) -> bool: ...

    def fail_task(
        self,
        task_id: uuid.UUID,
        worker_id: str,
        error: str,
        *,
        retry_base_seconds: int,
    ) -> FrontierStatus: ...

    def is_excluded(self, subject_type: str, subject_key: str, *, scope: str) -> bool: ...

    def add_exclusion(
        self,
        *,
        subject_type: str,
        subject_key: str,
        reason_code: str,
        source: str,
        scope: str = "all",
        reason: str = "",
        created_by: str | None = None,
    ) -> None: ...
