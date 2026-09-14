from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, Mapping

from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.bluesky.candidates import iter_author_image_candidates
from artsearch.bluesky.client import BlueskyClient
from artsearch.bluesky.config import BlueskyModerationConfig
from artsearch.discovery.models import (
    CreatorView,
    DiscoveryRequest,
    DiscoverySourceType,
    DiscoverySuggestion,
    GraphEdgeType,
    GraphEntityType,
)
from artsearch.discovery.repository import graph_entity_id


class BlueskyHydrator:
    def __init__(
        self,
        client: BlueskyClient,
        *,
        moderation: BlueskyModerationConfig,
    ) -> None:
        self.client = client
        self.moderation = moderation

    async def creator(self, actor: str) -> CreatorView:
        return creator_view_from_profile(await self.client.get_profile(actor))

    async def media_candidates(self, did: str, *, limit: int) -> list[ImageCandidate]:
        return [
            candidate
            async for candidate in iter_author_image_candidates(
                self.client,
                did,
                max_candidates=limit,
                moderation=self.moderation,
            )
        ]


class ManualSeedOracle:
    def __init__(self, actors: Sequence[str], *, source_ref: str) -> None:
        self.actors = tuple(dict.fromkeys(actor.strip() for actor in actors if actor.strip()))
        self.source_ref = source_ref

    async def suggest(
        self,
        request: DiscoveryRequest,
    ) -> AsyncIterator[DiscoverySuggestion]:
        parent_id = graph_entity_id(GraphEntityType.SOURCE, self.source_ref)
        for actor in self.actors[: request.limit]:
            yield DiscoverySuggestion(
                actor_ref=actor,
                source_type=DiscoverySourceType.MANUAL_SEED,
                source_ref=self.source_ref,
                graph_parent_id=parent_id,
                graph_parent_type=GraphEntityType.SOURCE,
                graph_parent_ref=self.source_ref,
                graph_edge_type=GraphEdgeType.SEARCH_HIT,
                depth=request.depth,
                confidence=1.0,
            )


class ActorSearchOracle:
    def __init__(self, client: BlueskyClient) -> None:
        self.client = client

    async def suggest(
        self,
        request: DiscoveryRequest,
    ) -> AsyncIterator[DiscoverySuggestion]:
        if not request.query:
            raise ValueError("actor search requires a query")
        page = await self.client.search_actors(
            request.query,
            limit=request.limit,
            cursor=request.cursor,
        )
        query_ref = f"actor-search:{request.query}"
        parent_id = graph_entity_id(GraphEntityType.QUERY, query_ref)
        for raw in page.items:
            profile = creator_view_from_profile(raw)
            yield DiscoverySuggestion(
                actor_ref=profile.did,
                profile=profile,
                source_type=DiscoverySourceType.ACTOR_SEARCH,
                source_ref=query_ref,
                graph_parent_id=parent_id,
                graph_parent_type=GraphEntityType.QUERY,
                graph_parent_ref=query_ref,
                graph_edge_type=GraphEdgeType.SEARCH_HIT,
                query=request.query,
                depth=request.depth,
                confidence=0.55,
            )


class PostSearchOracle:
    def __init__(self, client: BlueskyClient) -> None:
        self.client = client

    async def suggest(
        self,
        request: DiscoveryRequest,
    ) -> AsyncIterator[DiscoverySuggestion]:
        if not request.query:
            raise ValueError("post search requires a query")
        page = await self.client.search_posts(
            request.query,
            limit=request.limit,
            cursor=request.cursor,
        )
        query_ref = f"post-search:{request.query}"
        parent_id = graph_entity_id(GraphEntityType.QUERY, query_ref)
        seen: set[str] = set()
        for post in page.items:
            author = _mapping(post.get("author"))
            if author is None:
                continue
            profile = creator_view_from_profile(author)
            if profile.did in seen:
                continue
            seen.add(profile.did)
            yield DiscoverySuggestion(
                actor_ref=profile.did,
                profile=profile,
                source_type=DiscoverySourceType.POST_SEARCH,
                source_ref=_string(post.get("uri")) or query_ref,
                graph_parent_id=parent_id,
                graph_parent_type=GraphEntityType.QUERY,
                graph_parent_ref=query_ref,
                graph_edge_type=GraphEdgeType.SEARCH_HIT,
                query=request.query,
                depth=request.depth,
                confidence=0.65,
                metadata={"post_uri": _string(post.get("uri"))},
            )


class StarterPackSearchOracle:
    def __init__(self, client: BlueskyClient, *, max_members: int = 100) -> None:
        self.client = client
        self.max_members = max_members

    async def suggest(
        self,
        request: DiscoveryRequest,
    ) -> AsyncIterator[DiscoverySuggestion]:
        if not request.query:
            raise ValueError("Starter Pack search requires a query")
        packs = await self.client.search_starter_packs(
            request.query,
            limit=request.limit,
            cursor=request.cursor,
        )
        for basic in packs.items:
            pack_uri = _string(basic.get("uri"))
            if not pack_uri:
                continue
            pack = await self.client.get_starter_pack(pack_uri)
            list_view = _mapping(pack.get("list"))
            list_uri = _string(list_view.get("uri")) if list_view is not None else None
            sample = pack.get("listItemsSample")
            members: list[Mapping[str, Any]] = []
            seen_members: set[str] = set()

            def extend_members(values: Sequence[Mapping[str, Any]]) -> None:
                for value in values:
                    identity = _string(value.get("did")) or _string(value.get("handle"))
                    if not identity or identity in seen_members:
                        continue
                    seen_members.add(identity)
                    members.append(value)

            extend_members(_subjects(sample))
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while list_uri and len(members) < self.max_members:
                page = await self.client.get_list(
                    list_uri,
                    limit=min(100, self.max_members - len(members)),
                    cursor=cursor,
                )
                extend_members(_subjects(page.items))
                cursor = page.cursor
                if not cursor or cursor in seen_cursors or not page.items:
                    break
                seen_cursors.add(cursor)
            parent_id = graph_entity_id(GraphEntityType.STARTER_PACK, pack_uri)
            for raw in members[: self.max_members]:
                profile = creator_view_from_profile(raw)
                yield DiscoverySuggestion(
                    actor_ref=profile.did,
                    profile=profile,
                    source_type=DiscoverySourceType.STARTER_PACK,
                    source_ref=pack_uri,
                    graph_parent_id=parent_id,
                    graph_parent_type=GraphEntityType.STARTER_PACK,
                    graph_parent_ref=pack_uri,
                    graph_edge_type=GraphEdgeType.STARTER_PACK_MEMBER,
                    query=request.query,
                    depth=request.depth,
                    confidence=0.9,
                    metadata={
                        "starter_pack_name": _record_name(pack),
                        "list_uri": list_uri,
                    },
                )


class OutboundFollowOracle:
    def __init__(self, client: BlueskyClient) -> None:
        self.client = client

    async def suggest(
        self,
        request: DiscoveryRequest,
    ) -> AsyncIterator[DiscoverySuggestion]:
        if not request.actor:
            raise ValueError("outbound-follow expansion requires an actor")
        page = await self.client.get_follows(
            request.actor,
            limit=request.limit,
            cursor=request.cursor,
        )
        parent_id = graph_entity_id(GraphEntityType.CREATOR, request.actor)
        for raw in page.items:
            profile = creator_view_from_profile(raw)
            yield DiscoverySuggestion(
                actor_ref=profile.did,
                profile=profile,
                source_type=DiscoverySourceType.OUTBOUND_FOLLOW,
                source_ref=request.actor,
                graph_parent_id=parent_id,
                graph_parent_type=GraphEntityType.CREATOR,
                graph_parent_ref=request.actor,
                graph_edge_type=GraphEdgeType.FOLLOWS,
                depth=request.depth,
                confidence=0.75,
            )


class SuggestedFollowOracle:
    def __init__(self, client: BlueskyClient) -> None:
        self.client = client

    async def suggest(
        self,
        request: DiscoveryRequest,
    ) -> AsyncIterator[DiscoverySuggestion]:
        if not request.actor:
            raise ValueError("suggested-follow expansion requires an actor")
        page = await self.client.get_suggested_follows_by_actor(request.actor)
        parent_id = graph_entity_id(GraphEntityType.CREATOR, request.actor)
        for raw in page.items[: request.limit]:
            profile = creator_view_from_profile(raw)
            yield DiscoverySuggestion(
                actor_ref=profile.did,
                profile=profile,
                source_type=DiscoverySourceType.SUGGESTED_FOLLOW,
                source_ref=request.actor,
                graph_parent_id=parent_id,
                graph_parent_type=GraphEntityType.CREATOR,
                graph_parent_ref=request.actor,
                graph_edge_type=GraphEdgeType.SUGGESTED_FOLLOW,
                depth=request.depth,
                confidence=0.6,
            )


def creator_view_from_profile(value: Mapping[str, Any]) -> CreatorView:
    did = _string(value.get("did"))
    handle = _string(value.get("handle"))
    if not did or not handle:
        raise ValueError("Bluesky profile is missing DID or handle")
    return CreatorView(
        did=did,
        handle=handle,
        display_name=_string(value.get("displayName")) or "",
        description=_string(value.get("description")) or "",
        avatar_url=_string(value.get("avatar")),
        followers_count=_integer(value.get("followersCount")),
        follows_count=_integer(value.get("followsCount")),
        posts_count=_integer(value.get("postsCount")),
        labels=tuple(_label_values(value.get("labels"))),
        created_at=_datetime(value.get("createdAt")),
        indexed_at=_datetime(value.get("indexedAt")),
        metadata={
            "associated": dict(_mapping(value.get("associated")) or {}),
            "verification": dict(_mapping(value.get("verification")) or {}),
        },
    )


def _subjects(values: object) -> list[Mapping[str, Any]]:
    if not isinstance(values, list):
        return []
    subjects = []
    for item in values:
        mapping = _mapping(item)
        if mapping is None:
            continue
        subject = _mapping(mapping.get("subject"))
        if subject is not None:
            subjects.append(subject)
    return subjects


def _record_name(pack: Mapping[str, Any]) -> str | None:
    record = _mapping(pack.get("record"))
    return _string(record.get("name")) if record is not None else None


def _label_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    labels = []
    for item in value:
        mapping = _mapping(item)
        label = _string(mapping.get("val")) if mapping is not None else None
        if label and mapping.get("neg") is not True:
            labels.append(label.casefold())
    return list(dict.fromkeys(labels))


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
