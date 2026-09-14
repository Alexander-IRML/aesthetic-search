from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import asyncio
import httpx

from artsearch.bluesky.config import BlueskyConfig


class BlueskyAPIError(RuntimeError):
    """Raised when the public Bluesky AppView returns an unsuccessful response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class BlueskyRequestBudgetExceeded(BlueskyAPIError):
    """Raised before an HTTP request would exceed an explicit run budget."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"Bluesky request budget exhausted after {limit} HTTP requests",
            retryable=False,
        )
        self.limit = limit


class BlueskyRequestBudget:
    """Concurrency-safe counter for actual AppView HTTP request attempts."""

    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("Bluesky request budget must be positive")
        self.limit = limit
        self.used = 0
        self._lock = asyncio.Lock()

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    async def consume(self) -> None:
        async with self._lock:
            if self.used >= self.limit:
                raise BlueskyRequestBudgetExceeded(self.limit)
            self.used += 1


@dataclass(frozen=True)
class AuthorFeedPage:
    feed: list[Mapping[str, Any]]
    cursor: str | None


@dataclass(frozen=True)
class BlueskyPage:
    items: list[Mapping[str, Any]]
    cursor: str | None
    metadata: Mapping[str, Any]


class BlueskyClient:
    def __init__(
        self,
        config: BlueskyConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        request_budget: BlueskyRequestBudget | None = None,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ValueError("provide either http_client or transport, not both")
        self.config = config
        self.request_budget = request_budget
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=config.api.base_url,
            headers={"User-Agent": config.api.user_agent},
            timeout=httpx.Timeout(config.api.timeout_seconds),
            limits=httpx.Limits(
                max_connections=config.api.max_connections,
                max_keepalive_connections=config.api.max_keepalive_connections,
            ),
            transport=transport,
        )

    async def __aenter__(self) -> "BlueskyClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_author_feed(
        self,
        actor: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        feed_filter: str | None = None,
    ) -> AuthorFeedPage:
        page_limit = limit if limit is not None else self.config.api.page_limit
        if page_limit < 1 or page_limit > 100:
            raise ValueError("limit must be between 1 and 100")

        params = {
            "actor": actor,
            "filter": feed_filter or self.config.api.feed_filter,
            "limit": str(page_limit),
        }
        if cursor:
            params["cursor"] = cursor

        response = await self._request_with_retries(actor, params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise BlueskyAPIError(
                f"getAuthorFeed returned invalid JSON for {actor!r}",
                status_code=response.status_code,
            ) from exc
        feed = payload.get("feed", [])
        if not isinstance(feed, list):
            raise BlueskyAPIError("getAuthorFeed response did not contain a feed list")
        cursor_value = payload.get("cursor")
        return AuthorFeedPage(
            feed=[item for item in feed if isinstance(item, Mapping)],
            cursor=cursor_value if isinstance(cursor_value, str) else None,
        )

    async def get_profile(self, actor: str) -> Mapping[str, Any]:
        return await self._get_json(
            "/xrpc/app.bsky.actor.getProfile",
            context=f"profile {actor!r}",
            params={"actor": actor},
        )

    async def search_actors(
        self,
        query: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> BlueskyPage:
        return await self._paged_query(
            "/xrpc/app.bsky.actor.searchActors",
            context=f"actor search {query!r}",
            params={"q": query, "limit": str(_page_limit(limit))},
            item_key="actors",
            cursor=cursor,
        )

    async def search_posts(
        self,
        query: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
        sort: str = "latest",
    ) -> BlueskyPage:
        if sort not in {"latest", "top"}:
            raise ValueError("post search sort must be latest or top")
        return await self._paged_query(
            "/xrpc/app.bsky.feed.searchPosts",
            context=f"post search {query!r}",
            params={"q": query, "limit": str(_page_limit(limit)), "sort": sort},
            item_key="posts",
            cursor=cursor,
        )

    async def search_starter_packs(
        self,
        query: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> BlueskyPage:
        return await self._paged_query(
            "/xrpc/app.bsky.graph.searchStarterPacks",
            context=f"Starter Pack search {query!r}",
            params={"q": query, "limit": str(_page_limit(limit))},
            item_key="starterPacks",
            cursor=cursor,
        )

    async def get_starter_pack(self, uri: str) -> Mapping[str, Any]:
        payload = await self._get_json(
            "/xrpc/app.bsky.graph.getStarterPack",
            context=f"Starter Pack {uri!r}",
            params={"starterPack": uri},
        )
        starter_pack = payload.get("starterPack")
        if not isinstance(starter_pack, Mapping):
            raise BlueskyAPIError("getStarterPack response did not contain a Starter Pack")
        return starter_pack

    async def get_list(
        self,
        uri: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> BlueskyPage:
        return await self._paged_query(
            "/xrpc/app.bsky.graph.getList",
            context=f"list {uri!r}",
            params={"list": uri, "limit": str(_page_limit(limit))},
            item_key="items",
            cursor=cursor,
        )

    async def get_follows(
        self,
        actor: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> BlueskyPage:
        return await self._paged_query(
            "/xrpc/app.bsky.graph.getFollows",
            context=f"outbound follows for {actor!r}",
            params={"actor": actor, "limit": str(_page_limit(limit))},
            item_key="follows",
            cursor=cursor,
        )

    async def get_suggested_follows_by_actor(self, actor: str) -> BlueskyPage:
        return await self._paged_query(
            "/xrpc/app.bsky.graph.getSuggestedFollowsByActor",
            context=f"suggested follows for {actor!r}",
            params={"actor": actor},
            item_key="suggestions",
        )

    async def _paged_query(
        self,
        endpoint: str,
        *,
        context: str,
        params: dict[str, str],
        item_key: str,
        cursor: str | None = None,
    ) -> BlueskyPage:
        if cursor:
            params["cursor"] = cursor
        payload = await self._get_json(endpoint, context=context, params=params)
        raw_items = payload.get(item_key, [])
        if not isinstance(raw_items, list):
            raise BlueskyAPIError(f"{context} response did not contain a {item_key} list")
        next_cursor = payload.get("cursor")
        return BlueskyPage(
            items=[item for item in raw_items if isinstance(item, Mapping)],
            cursor=next_cursor if isinstance(next_cursor, str) else None,
            metadata=payload,
        )

    async def _get_json(
        self,
        endpoint: str,
        *,
        context: str,
        params: dict[str, str],
    ) -> Mapping[str, Any]:
        response = await self._request_endpoint_with_retries(endpoint, context, params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise BlueskyAPIError(
                f"{context} returned invalid JSON",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, Mapping):
            raise BlueskyAPIError(f"{context} response was not a JSON object")
        return payload

    async def _request_with_retries(
        self,
        actor: str,
        params: dict[str, str],
    ) -> httpx.Response:
        return await self._request_endpoint_with_retries(
            "/xrpc/app.bsky.feed.getAuthorFeed",
            f"author feed for {actor!r}",
            params,
            actor=actor,
        )

    async def _request_endpoint_with_retries(
        self,
        endpoint: str,
        context: str,
        params: dict[str, str],
        *,
        actor: str | None = None,
    ) -> httpx.Response:
        for attempt in range(self.config.api.max_retries + 1):
            try:
                if self.request_budget is not None:
                    await self.request_budget.consume()
                response = await self._client.get(endpoint, params=params)
            except httpx.HTTPError as exc:
                if attempt < self.config.api.max_retries:
                    await asyncio.sleep(_retry_delay(self.config, attempt))
                    continue
                raise BlueskyAPIError(
                    f"Bluesky request failed for {context}: {exc}",
                    retryable=True,
                ) from exc

            retryable = _retryable_status(response.status_code)
            if retryable and attempt < self.config.api.max_retries:
                await asyncio.sleep(_retry_delay(self.config, attempt, response=response))
                continue
            if response.status_code >= 400:
                raise BlueskyAPIError(
                    _response_error_message(response, actor or context, context=context),
                    status_code=response.status_code,
                    retryable=retryable,
                )
            return response
        raise BlueskyAPIError(f"Bluesky request retries exhausted for {context}", retryable=True)

    async def iter_author_feed(
        self,
        actor: str,
        *,
        cursor: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
        feed_filter: str | None = None,
    ) -> AsyncIterator[AuthorFeedPage]:
        page_count = 0
        next_cursor = cursor
        page_cap = max_pages if max_pages is not None else self.config.api.max_pages
        if page_cap < 1:
            raise ValueError("max_pages must be positive")

        while page_count < page_cap:
            page = await self.get_author_feed(
                actor,
                cursor=next_cursor,
                limit=limit,
                feed_filter=feed_filter,
            )
            yield page
            page_count += 1
            if not page.cursor:
                break
            next_cursor = page.cursor


def _response_error_message(
    response: httpx.Response,
    actor: str,
    *,
    context: str | None = None,
) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = payload.get("message") if isinstance(payload, Mapping) else None
    if response.status_code in {400, 404} and detail == "Profile not found":
        return f"Bluesky profile not found: {actor!r}; pass a real handle or DID"
    description = str(detail) if detail else response.text[:300]
    operation = context or f"author feed for {actor!r}"
    return f"Bluesky request failed for {operation}: {response.status_code} {description}"


def _page_limit(value: int) -> int:
    if value < 1 or value > 100:
        raise ValueError("limit must be between 1 and 100")
    return value


def _retryable_status(status: int) -> bool:
    return status in {408, 425, 429} or status >= 500


def _retry_delay(
    config: BlueskyConfig,
    attempt: int,
    *,
    response: httpx.Response | None = None,
) -> float:
    retry_after = response.headers.get("retry-after") if response is not None else None
    requested = _retry_after_seconds(retry_after)
    delay = requested if requested is not None else config.api.retry_backoff_seconds * (2**attempt)
    return min(max(0.0, delay), config.api.retry_backoff_max_seconds)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
