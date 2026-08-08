from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, Field

from artsearch.artwork_filter.errors import PersistenceError
from artsearch.bluesky.candidates import (
    candidates_from_feed_item,
    moderation_reasons_from_feed_item,
)
from artsearch.bluesky.client import BlueskyAPIError, BlueskyClient
from artsearch.bluesky.config import BlueskyModerationConfig


FEMININE_TERMS = {
    "daughter",
    "female",
    "girl",
    "girls",
    "girlfriend",
    "her",
    "heroine",
    "lady",
    "ladies",
    "mother",
    "princess",
    "queen",
    "she",
    "wife",
    "woman",
    "women",
}
MASCULINE_TERMS = {
    "boy",
    "boys",
    "boyfriend",
    "father",
    "he",
    "hero",
    "him",
    "husband",
    "king",
    "male",
    "man",
    "men",
    "prince",
    "son",
}


class ActorAudit(BaseModel):
    actor: str
    status: Literal["ok", "error"]
    error: str | None = None
    feed_items: int = 0
    image_posts: int = 0
    image_candidates: int = 0
    allowed_candidates: int = 0
    excluded_image_posts: int = 0
    label_excluded_posts: int = 0
    text_excluded_posts: int = 0
    feminine_signal_posts: int = 0
    masculine_signal_posts: int = 0
    mixed_signal_posts: int = 0
    excluded_fraction: float = Field(ge=0.0, le=1.0)


async def audit_actors(
    client: BlueskyClient,
    actors: Sequence[str],
    *,
    moderation: BlueskyModerationConfig,
    limit: int = 50,
    concurrency: int = 6,
) -> list[ActorAudit]:
    if not 1 <= limit <= 100:
        raise ValueError("audit limit must be between 1 and 100")
    if concurrency <= 0:
        raise ValueError("audit concurrency must be positive")
    semaphore = asyncio.Semaphore(concurrency)

    async def audit_one(actor: str) -> ActorAudit:
        async with semaphore:
            try:
                page = await client.get_author_feed(
                    actor,
                    limit=limit,
                    feed_filter="posts_with_media",
                )
            except BlueskyAPIError as exc:
                return ActorAudit(
                    actor=actor,
                    status="error",
                    error=str(exc),
                    excluded_fraction=0.0,
                )
            return audit_feed_items(
                actor,
                page.feed,
                moderation=moderation,
            )

    return list(await asyncio.gather(*(audit_one(actor) for actor in actors)))


def audit_feed_items(
    actor: str,
    items: Sequence[dict],
    *,
    moderation: BlueskyModerationConfig,
) -> ActorAudit:
    counts = {
        "image_posts": 0,
        "image_candidates": 0,
        "allowed_candidates": 0,
        "excluded_image_posts": 0,
        "label_excluded_posts": 0,
        "text_excluded_posts": 0,
        "feminine_signal_posts": 0,
        "masculine_signal_posts": 0,
        "mixed_signal_posts": 0,
    }
    for item in items:
        raw_candidates = candidates_from_feed_item(item)
        if not raw_candidates:
            continue
        counts["image_posts"] += 1
        counts["image_candidates"] += len(raw_candidates)
        reasons = moderation_reasons_from_feed_item(item, moderation=moderation)
        if reasons:
            counts["excluded_image_posts"] += 1
            if any(reason.startswith("label:") for reason in reasons):
                counts["label_excluded_posts"] += 1
            if any(reason.startswith("text:") for reason in reasons):
                counts["text_excluded_posts"] += 1
            continue

        safe_candidates = candidates_from_feed_item(item, moderation=moderation)
        counts["allowed_candidates"] += len(safe_candidates)
        words = _subject_words(raw_candidates)
        feminine = bool(words & FEMININE_TERMS)
        masculine = bool(words & MASCULINE_TERMS)
        counts["feminine_signal_posts"] += int(feminine)
        counts["masculine_signal_posts"] += int(masculine)
        counts["mixed_signal_posts"] += int(feminine and masculine)

    image_posts = counts["image_posts"]
    excluded_fraction = counts["excluded_image_posts"] / image_posts if image_posts else 0.0
    return ActorAudit(
        actor=actor,
        status="ok",
        feed_items=len(items),
        excluded_fraction=excluded_fraction,
        **counts,
    )


def select_safe_pilot(
    audits: Sequence[ActorAudit],
    *,
    count: int,
    min_allowed_candidates: int = 3,
    max_excluded_fraction: float = 0.10,
) -> list[str]:
    if count <= 0:
        raise ValueError("pilot count must be positive")
    if min_allowed_candidates <= 0:
        raise ValueError("minimum allowed candidates must be positive")
    if not 0.0 <= max_excluded_fraction <= 1.0:
        raise ValueError("maximum excluded fraction must be in [0, 1]")

    eligible = [
        audit
        for audit in audits
        if audit.status == "ok"
        and audit.allowed_candidates >= min_allowed_candidates
        and audit.excluded_fraction <= max_excluded_fraction
        and not _masculine_skew(audit)
    ]
    eligible.sort(key=_pilot_priority)
    return [audit.actor for audit in eligible[:count]]


def write_audit_report(audits: Sequence[ActorAudit], path: str | Path) -> Path:
    destination = Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "\n".join(audit.model_dump_json() for audit in audits) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise PersistenceError(f"could not write actor audit: {exc}") from exc
    return destination


def write_actor_file(actors: Sequence[str], path: str | Path) -> Path:
    destination = Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "# Metadata-audited public-safe Bluesky pilot roster\n" + "\n".join(actors) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise PersistenceError(f"could not write pilot actor file: {exc}") from exc
    return destination


def _subject_words(candidates: Sequence) -> set[str]:
    text = "\n".join([candidates[0].post_text, *(candidate.alt_text for candidate in candidates)])
    return set(re.findall(r"[a-z]+", text.lower()))


def _masculine_skew(audit: ActorAudit) -> bool:
    return audit.masculine_signal_posts > audit.feminine_signal_posts


def _pilot_priority(audit: ActorAudit) -> tuple[int, float, int, int, str]:
    if audit.feminine_signal_posts > audit.masculine_signal_posts:
        subject_rank = 0
    elif audit.mixed_signal_posts > 0 or (
        audit.feminine_signal_posts > 0 and audit.masculine_signal_posts > 0
    ):
        subject_rank = 1
    elif audit.feminine_signal_posts > 0:
        subject_rank = 2
    else:
        subject_rank = 3
    return (
        subject_rank,
        audit.excluded_fraction,
        -audit.feminine_signal_posts,
        -audit.allowed_candidates,
        audit.actor,
    )
