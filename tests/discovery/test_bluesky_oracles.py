from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from artsearch.bluesky.client import BlueskyClient, BlueskyPage
from artsearch.bluesky.config import BlueskyConfig
from artsearch.discovery.bluesky import (
    ActorSearchOracle,
    StarterPackSearchOracle,
    creator_view_from_profile,
)
from artsearch.discovery.models import DiscoveryRequest, DiscoverySourceType, GraphEdgeType


def test_client_exposes_discovery_xrpc_surfaces_with_bounded_limits() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("searchActors"):
            return httpx.Response(200, json={"actors": [_profile("one")]}, request=request)
        if path.endswith("searchPosts"):
            return httpx.Response(200, json={"posts": []}, request=request)
        if path.endswith("searchStarterPacks"):
            return httpx.Response(200, json={"starterPacks": []}, request=request)
        if path.endswith("getFollows"):
            return httpx.Response(200, json={"follows": []}, request=request)
        return httpx.Response(200, json={"suggestions": []}, request=request)

    async def run() -> None:
        config = BlueskyConfig.model_validate({"api": {"base_url": "https://example.test"}})
        async with BlueskyClient(config, transport=httpx.MockTransport(handler)) as client:
            await client.search_actors("artist", limit=20)
            await client.search_posts("my art", limit=21)
            await client.search_starter_packs("artists", limit=10)
            await client.get_follows("did:plc:one", limit=22)
            await client.get_suggested_follows_by_actor("did:plc:one")

    asyncio.run(run())

    assert [request.url.path.rsplit(".", 1)[-1] for request in requests] == [
        "searchActors",
        "searchPosts",
        "searchStarterPacks",
        "getFollows",
        "getSuggestedFollowsByActor",
    ]
    assert requests[0].url.params["limit"] == "20"
    assert requests[1].url.params["sort"] == "latest"


def test_actor_search_preserves_query_provenance() -> None:
    class Client:
        async def search_actors(self, query: str, **_: object) -> BlueskyPage:
            return BlueskyPage([_profile("one")], None, {})

    async def run():
        oracle = ActorSearchOracle(Client())  # type: ignore[arg-type]
        return [item async for item in oracle.suggest(DiscoveryRequest(query="pixel artist"))]

    suggestions = asyncio.run(run())

    assert len(suggestions) == 1
    assert suggestions[0].actor_ref == "did:plc:one"
    assert suggestions[0].source_type == DiscoverySourceType.ACTOR_SEARCH
    assert suggestions[0].graph_edge_type == GraphEdgeType.SEARCH_HIT
    assert suggestions[0].query == "pixel artist"


def test_starter_pack_members_are_capped_per_pack_and_paginated() -> None:
    class Client:
        async def search_starter_packs(self, *_: object, **__: object) -> BlueskyPage:
            return BlueskyPage(
                [{"uri": "at://pack/one"}, {"uri": "at://pack/two"}],
                None,
                {},
            )

        async def get_starter_pack(self, uri: str) -> Mapping[str, Any]:
            return {"uri": uri, "list": {"uri": f"{uri}/list"}, "listItemsSample": []}

        async def get_list(self, uri: str, **_: object) -> BlueskyPage:
            suffix = "one" if "/one/" in uri else "two"
            return BlueskyPage(
                [{"subject": _profile(f"{suffix}-{index}")} for index in range(3)],
                None,
                {},
            )

    async def run():
        oracle = StarterPackSearchOracle(Client(), max_members=2)  # type: ignore[arg-type]
        return [
            item async for item in oracle.suggest(DiscoveryRequest(query="small artists", limit=2))
        ]

    suggestions = asyncio.run(run())

    assert len(suggestions) == 4
    assert {item.source_ref for item in suggestions} == {"at://pack/one", "at://pack/two"}
    assert all(item.graph_edge_type == GraphEdgeType.STARTER_PACK_MEMBER for item in suggestions)


def test_starter_pack_stops_on_repeated_cursor() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def search_starter_packs(self, *_: object, **__: object) -> BlueskyPage:
            return BlueskyPage([{"uri": "at://pack/repeating"}], None, {})

        async def get_starter_pack(self, uri: str) -> Mapping[str, Any]:
            return {"uri": uri, "list": {"uri": f"{uri}/list"}, "listItemsSample": []}

        async def get_list(self, *_: object, **__: object) -> BlueskyPage:
            self.calls += 1
            return BlueskyPage(
                [{"subject": _profile(f"artist-{self.calls}")}],
                "same-cursor",
                {},
            )

    async def run(client: Client):
        oracle = StarterPackSearchOracle(client, max_members=10)  # type: ignore[arg-type]
        return [item async for item in oracle.suggest(DiscoveryRequest(query="artists", limit=1))]

    client = Client()
    suggestions = asyncio.run(run(client))

    assert client.calls == 2
    assert len(suggestions) == 2


def test_starter_pack_pagination_counts_unique_sample_members() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def search_starter_packs(self, *_: object, **__: object) -> BlueskyPage:
            return BlueskyPage([{"uri": "at://pack/overlap"}], None, {})

        async def get_starter_pack(self, uri: str) -> Mapping[str, Any]:
            return {
                "uri": uri,
                "list": {"uri": f"{uri}/list"},
                "listItemsSample": [{"subject": _profile("artist-0")}],
            }

        async def get_list(self, *_: object, **__: object) -> BlueskyPage:
            self.calls += 1
            if self.calls == 1:
                return BlueskyPage(
                    [
                        {"subject": _profile("artist-0")},
                        {"subject": _profile("artist-1")},
                    ],
                    "next",
                    {},
                )
            return BlueskyPage([{"subject": _profile("artist-2")}], None, {})

    async def run(client: Client):
        oracle = StarterPackSearchOracle(client, max_members=3)  # type: ignore[arg-type]
        return [item async for item in oracle.suggest(DiscoveryRequest(query="artists", limit=1))]

    client = Client()
    suggestions = asyncio.run(run(client))

    assert client.calls == 2
    assert [item.actor_ref for item in suggestions] == [
        "did:plc:artist-0",
        "did:plc:artist-1",
        "did:plc:artist-2",
    ]


def test_profile_parser_uses_did_and_active_labels() -> None:
    profile = creator_view_from_profile(
        {
            **_profile("artist"),
            "labels": [{"val": "porn"}, {"val": "old", "neg": True}],
            "followersCount": 12,
        }
    )

    assert profile.did == "did:plc:artist"
    assert profile.labels == ("porn",)
    assert profile.followers_count == 12


def _profile(name: str) -> dict[str, object]:
    return {
        "did": f"did:plc:{name}",
        "handle": f"{name}.test",
        "displayName": name.title(),
        "description": "independent artist",
    }
