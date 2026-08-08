import asyncio

import httpx
import pytest

from artsearch.bluesky.client import BlueskyAPIError, BlueskyClient
from artsearch.bluesky.config import BlueskyConfig


def test_client_paginates_author_feed_with_public_appview_endpoint():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"feed": [{"post": {"uri": "first"}}], "cursor": "c2"})
        return httpx.Response(200, json={"feed": [{"post": {"uri": "second"}}]})

    async def run():
        config = BlueskyConfig.model_validate(
            {"api": {"base_url": "https://example.test", "page_limit": 50}}
        )
        async with BlueskyClient(config, transport=httpx.MockTransport(handler)) as client:
            return [
                page
                async for page in client.iter_author_feed(
                    "did:plc:artist",
                    max_pages=2,
                    limit=25,
                )
            ]

    pages = asyncio.run(run())

    assert len(pages) == 2
    assert pages[0].feed[0]["post"]["uri"] == "first"
    assert pages[1].feed[0]["post"]["uri"] == "second"
    assert requests[0].url.path == "/xrpc/app.bsky.feed.getAuthorFeed"
    assert requests[0].url.params["actor"] == "did:plc:artist"
    assert requests[0].url.params["filter"] == "posts_with_media"
    assert requests[0].url.params["limit"] == "25"
    assert requests[1].url.params["cursor"] == "c2"


def test_client_raises_typed_error_on_http_failure():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="too many requests")

    async def run():
        config = BlueskyConfig.model_validate(
            {"api": {"base_url": "https://example.test", "max_retries": 0}}
        )
        async with BlueskyClient(config, transport=httpx.MockTransport(handler)) as client:
            await client.get_author_feed("did:plc:artist")

    with pytest.raises(BlueskyAPIError):
        asyncio.run(run())


def test_client_explains_missing_profile_without_raw_response_dump():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "InvalidRequest", "message": "Profile not found"},
        )

    async def run():
        config = BlueskyConfig.model_validate({"api": {"base_url": "https://example.test"}})
        async with BlueskyClient(config, transport=httpx.MockTransport(handler)) as client:
            await client.get_author_feed("missing.example")

    with pytest.raises(BlueskyAPIError, match="Bluesky profile not found: 'missing.example'"):
        asyncio.run(run())


def test_client_retries_rate_limit_and_server_failure_before_success():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        if len(requests) == 2:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"feed": []}, request=request)

    async def run():
        config = BlueskyConfig.model_validate(
            {
                "api": {
                    "base_url": "https://example.test",
                    "max_retries": 2,
                    "retry_backoff_seconds": 0,
                }
            }
        )
        async with BlueskyClient(config, transport=httpx.MockTransport(handler)) as client:
            return await client.get_author_feed("did:plc:artist")

    page = asyncio.run(run())

    assert page.feed == []
    assert len(requests) == 3


def test_client_wraps_invalid_success_payload_as_typed_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json", request=request)

    async def run():
        config = BlueskyConfig.model_validate({"api": {"base_url": "https://example.test"}})
        async with BlueskyClient(config, transport=httpx.MockTransport(handler)) as client:
            await client.get_author_feed("did:plc:artist")

    with pytest.raises(BlueskyAPIError, match="returned invalid JSON"):
        asyncio.run(run())
