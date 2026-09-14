from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
import uuid

from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate


class GraphEntityType(StrEnum):
    CREATOR = "creator"
    POST = "post"
    MEDIA = "media"
    STARTER_PACK = "starter_pack"
    LIST = "list"
    FEED = "feed"
    QUERY = "query"
    SOURCE = "source"


class GraphEdgeType(StrEnum):
    STARTER_PACK_MEMBER = "starter_pack_member"
    LIST_MEMBER = "list_member"
    FOLLOWS = "follows"
    FOLLOWED_BY = "followed_by"
    AUTHORED = "authored"
    ATTACHED_MEDIA = "attached_media"
    REPLIED_TO = "replied_to"
    QUOTED = "quoted"
    MENTIONED = "mentioned"
    REPOSTED = "reposted"
    LIKED = "liked"
    GRAPH_SIMILAR = "graph_similar"
    OBSERVED_IN_FEED = "observed_in_feed"
    SEARCH_HIT = "search_hit"
    SUGGESTED_FOLLOW = "suggested_follow"
    VISUALLY_SIMILAR = "visually_similar"
    TAXONOMICALLY_SIMILAR = "taxonomically_similar"


class DiscoverySourceType(StrEnum):
    MANUAL_SEED = "manual_seed"
    ACTOR_SEARCH = "actor_search"
    POST_SEARCH = "post_search"
    STARTER_PACK_SEARCH = "starter_pack_search"
    STARTER_PACK = "starter_pack"
    LIST = "list"
    OUTBOUND_FOLLOW = "outbound_follow"
    SUGGESTED_FOLLOW = "suggested_follow"
    FEED = "feed"
    JETSTREAM = "jetstream"


class CreatorState(StrEnum):
    DISCOVERED = "discovered"
    HYDRATED = "hydrated"
    SAMPLED = "sampled"
    NEEDS_REVIEW = "needs_review"
    QUALIFIED = "qualified"
    DEFERRED = "deferred"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    DELETED = "deleted"


class MediaState(StrEnum):
    OBSERVED = "observed"
    FILTERED = "filtered"
    ACCEPTED = "accepted"
    REVIEW = "review"
    REJECTED = "rejected"
    INDEXED = "indexed"
    DELETED = "deleted"
    ERROR = "error"


class SafetyState(StrEnum):
    UNKNOWN = "unknown"
    ALLOWED = "allowed"
    REVIEW = "review"
    BLOCKED = "blocked"


class ExpansionAction(StrEnum):
    SAMPLE_CREATOR_MEDIA = "sample_creator_media"
    EXPAND_OUTBOUND_FOLLOWS = "expand_outbound_follows"
    EXPAND_SUGGESTED_FOLLOWS = "expand_suggested_follows"
    EXPAND_STARTER_PACK = "expand_starter_pack"
    EXPAND_LIST = "expand_list"
    SAMPLE_FEED = "sample_feed"


class FrontierStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY = "retry"
    COMPLETED = "completed"
    DEAD = "dead"
    CANCELLED = "cancelled"


class CampaignState(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    DEFER = "defer"
    REJECT = "reject"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class CreatorView:
    did: str
    handle: str
    display_name: str = ""
    description: str = ""
    avatar_url: str | None = None
    followers_count: int | None = None
    follows_count: int | None = None
    posts_count: int | None = None
    labels: tuple[str, ...] = ()
    created_at: datetime | None = None
    indexed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    query: str | None = None
    actor: str | None = None
    cursor: str | None = None
    limit: int = 25
    depth: int = 0
    campaign_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class DiscoverySuggestion:
    actor_ref: str
    source_type: DiscoverySourceType
    source_ref: str
    profile: CreatorView | None = None
    graph_parent_id: str | None = None
    graph_parent_type: GraphEntityType | None = None
    graph_parent_ref: str | None = None
    graph_edge_type: GraphEdgeType | None = None
    query: str | None = None
    depth: int = 0
    confidence: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NetworkEvent:
    event_id: str
    operation: str
    collection: str
    repository_did: str
    record_uri: str | None
    record_cid: str | None
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClassificationEvidence:
    entity_type: GraphEntityType
    entity_id: str
    namespace: str
    label: str
    score: float
    confidence: float | None
    score_kind: str
    model_name: str
    model_version: str
    config_hash: str
    calibration_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CreatorProfile:
    did: str
    state: CreatorState
    safety_state: SafetyState
    creator_score: float
    expansion_score: float
    score_version: str
    score_components: dict[str, float]
    sampled_media: int
    accepted_media: int
    review_media: int
    rejected_media: int
    error_media: int
    profile_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CreatorSamplingStatus:
    state: CreatorState
    safety_state: SafetyState
    score_version: str | None
    expansion_score: float | None
    media_sampled_at: datetime | None


@dataclass(frozen=True, slots=True)
class MediaBatchOutcome:
    candidates: tuple[ImageCandidate, ...]
    results: tuple[FilterResult, ...]
    pipeline_summary: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FrontierTask:
    task_id: uuid.UUID
    campaign_id: uuid.UUID
    entity_id: str
    action: ExpansionAction
    status: FrontierStatus
    depth: int
    priority: float
    budget_class: str
    attempts: int
    max_attempts: int
    run_after: datetime
    parent_task_id: uuid.UUID | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DiscoveryRunSummary:
    campaign_id: str = ""
    observations: int = 0
    unique_creators: int = 0
    duplicate_creator_ids: int = 0
    duplicate_expansion_account_ids: int = 0
    profiles_hydrated: int = 0
    creators_sampled: int = 0
    creators_deepened: int = 0
    creators_qualified: int = 0
    creators_review: int = 0
    creators_rejected: int = 0
    creators_blocked: int = 0
    tasks_claimed: int = 0
    tasks_completed: int = 0
    tasks_retried: int = 0
    tasks_dead: int = 0
    expansion_candidates_added: int = 0
    expansion_nodes_at_limit: int = 0
    candidates: int = 0
    accepted: int = 0
    review: int = 0
    rejected: int = 0
    classification_errors: int = 0
    routed: int = 0
    imported: int = 0
    duplicates: int = 0
    review_stored: int = 0
    route_errors: int = 0
    source_errors: int = 0
    budget_reached: int = 0
    api_requests: int = 0
    api_request_budget: int = 0

    @property
    def has_errors(self) -> bool:
        return bool(self.source_errors or self.classification_errors or self.route_errors)

    def to_dict(self) -> dict[str, int | str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}
