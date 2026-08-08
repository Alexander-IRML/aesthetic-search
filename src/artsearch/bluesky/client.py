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


@dataclass(frozen=True)
class AuthorFeedPage:
    feed: list[Mapping[str, Any]]
    cursor: str | None


class BlueskyClient:
    def __init__(
        self,
        config: BlueskyConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ValueError("provide either http_client or transport, not both")
        self.config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=config.api.base_url,
            headers={"User-Agent": config.api.user_agent},
            timeout=config.api.timeout_seconds,
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

    async def _request_with_retries(
        self,
        actor: str,
        params: dict[str, str],
    ) -> httpx.Response:
        for attempt in range(self.config.api.max_retries + 1):
            try:
                response = await self._client.get(
                    "/xrpc/app.bsky.feed.getAuthorFeed",
                    params=params,
                )
            except httpx.HTTPError as exc:
                if attempt < self.config.api.max_retries:
                    await asyncio.sleep(_retry_delay(self.config, attempt))
                    continue
                raise BlueskyAPIError(
                    f"Bluesky request failed for {actor!r}: {exc}",
                    retryable=True,
                ) from exc

            retryable = _retryable_status(response.status_code)
            if retryable and attempt < self.config.api.max_retries:
                await asyncio.sleep(_retry_delay(self.config, attempt, response=response))
                continue
            if response.status_code >= 400:
                raise BlueskyAPIError(
                    _response_error_message(response, actor),
                    status_code=response.status_code,
                    retryable=retryable,
                )
            return response
        raise BlueskyAPIError(f"Bluesky request retries exhausted for {actor!r}", retryable=True)

    async def iter_author_feed(
        self,
        actor: str,
        *,
        max_pages: int | None = None,
        limit: int | None = None,
        feed_filter: str | None = None,
    ) -> AsyncIterator[AuthorFeedPage]:
        page_count = 0
        cursor: str | None = None
        page_cap = max_pages if max_pages is not None else self.config.api.max_pages
        if page_cap < 1:
            raise ValueError("max_pages must be positive")

        while page_count < page_cap:
            page = await self.get_author_feed(
                actor,
                cursor=cursor,
                limit=limit,
                feed_filter=feed_filter,
            )
            yield page
            page_count += 1
            if not page.cursor:
                break
            cursor = page.cursor


def _response_error_message(response: httpx.Response, actor: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = payload.get("message") if isinstance(payload, Mapping) else None
    if response.status_code in {400, 404} and detail == "Profile not found":
        return f"Bluesky profile not found: {actor!r}; pass a real handle or DID"
    description = str(detail) if detail else response.text[:300]
    return f"getAuthorFeed failed for {actor!r}: {response.status_code} {description}"


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
    delay = (
        requested
        if requested is not None
        else config.api.retry_backoff_seconds * (2**attempt)
    )
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
