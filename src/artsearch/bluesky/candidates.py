from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import re
from typing import Any
import unicodedata

from artsearch.artwork_filter.hashing import make_candidate_id
from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.bluesky.client import BlueskyClient
from artsearch.bluesky.config import BlueskyModerationConfig


def candidates_from_feed_item(
    item: Mapping[str, Any],
    *,
    moderation: BlueskyModerationConfig | None = None,
) -> list[ImageCandidate]:
    post = _mapping(item.get("post"))
    if post is None:
        return []

    post_uri = _string(post.get("uri"))
    if not post_uri:
        return []

    author = _mapping(post.get("author")) or {}
    record = _mapping(post.get("record")) or {}
    embed = _mapping(post.get("embed")) or {}
    images = _image_views_from_embed(embed)
    if not images:
        return []
    if moderation_reasons_from_feed_item(item, moderation=moderation):
        return []

    is_quote_post, quoted_author_did = _quote_metadata(embed)
    is_repost = _is_repost(item)
    created_at = _string(record.get("createdAt")) or _string(post.get("indexedAt"))
    langs = record.get("langs")
    content_labels = _dedupe(
        [
            *_active_label_values(post.get("labels")),
            *_self_label_values(record.get("labels")),
        ]
    )
    author_labels = _active_label_values(author.get("labels"))

    candidates = []
    for image_index, image in enumerate(images):
        thumbnail_url = _string(image.get("thumb"))
        fullsize_url = _string(image.get("fullsize"))
        if not thumbnail_url and not fullsize_url:
            continue
        aspect_ratio = _mapping(image.get("aspectRatio")) or {}
        candidates.append(
            ImageCandidate(
                candidate_id=make_candidate_id(post_uri, image_index),
                author_did=_string(author.get("did")),
                author_handle=_string(author.get("handle")),
                post_uri=post_uri,
                post_cid=_string(post.get("cid")),
                image_index=image_index,
                thumbnail_url=thumbnail_url,
                fullsize_url=fullsize_url,
                post_text=_string(record.get("text")) or "",
                alt_text=_string(image.get("alt")) or "",
                created_at=created_at,
                langs=[value for value in langs if isinstance(value, str)]
                if isinstance(langs, list)
                else [],
                content_labels=content_labels,
                author_labels=author_labels,
                is_repost=is_repost,
                is_quote_post=is_quote_post,
                quoted_author_did=quoted_author_did,
                declared_width=_int_or_none(aspect_ratio.get("width")),
                declared_height=_int_or_none(aspect_ratio.get("height")),
                source="bluesky",
            )
        )
    return candidates


async def iter_author_image_candidates(
    client: BlueskyClient,
    actor: str,
    *,
    max_pages: int | None = None,
    limit: int | None = None,
    feed_filter: str | None = None,
    moderation: BlueskyModerationConfig | None = None,
) -> AsyncIterator[ImageCandidate]:
    async for page in client.iter_author_feed(
        actor,
        max_pages=max_pages,
        limit=limit,
        feed_filter=feed_filter,
    ):
        for item in page.feed:
            for candidate in candidates_from_feed_item(item, moderation=moderation):
                yield candidate


def moderation_reasons_from_feed_item(
    item: Mapping[str, Any],
    *,
    moderation: BlueskyModerationConfig | None,
) -> list[str]:
    if moderation is None or not moderation.public_safe_mode:
        return []
    post = _mapping(item.get("post")) or {}
    author = _mapping(post.get("author")) or {}
    record = _mapping(post.get("record")) or {}
    embed = _mapping(post.get("embed")) or {}
    content_labels = _dedupe(
        [
            *_active_label_values(post.get("labels")),
            *_self_label_values(record.get("labels")),
        ]
    )
    author_labels = _active_label_values(author.get("labels"))
    excluded_labels = set(moderation.excluded_labels)
    reasons = [
        f"label:{label}"
        for label in _dedupe([*content_labels, *author_labels])
        if label in excluded_labels
    ]

    images = _image_views_from_embed(embed)
    text = "\n".join(
        [
            _string(record.get("text")) or "",
            *(_string(image.get("alt")) or "" for image in images),
        ]
    )
    normalized_text = unicodedata.normalize("NFKC", text).lower()
    reasons.extend(
        f"text:{term}"
        for term in moderation.excluded_text_terms
        if _contains_term(normalized_text, term)
    )
    return _dedupe(reasons)


def _image_views_from_embed(embed: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    embed_type = _string(embed.get("$type"))
    if embed_type == "app.bsky.embed.images#view":
        images = embed.get("images", [])
        return (
            [image for image in images if isinstance(image, Mapping)]
            if isinstance(images, list)
            else []
        )
    if embed_type == "app.bsky.embed.recordWithMedia#view":
        media = _mapping(embed.get("media"))
        return _image_views_from_embed(media) if media is not None else []
    return []


def _quote_metadata(embed: Mapping[str, Any]) -> tuple[bool, str | None]:
    embed_type = _string(embed.get("$type"))
    if embed_type not in {
        "app.bsky.embed.record#view",
        "app.bsky.embed.recordWithMedia#view",
    }:
        return False, None
    record = _mapping(embed.get("record")) or {}
    author = _mapping(record.get("author")) or {}
    return True, _string(author.get("did"))


def _is_repost(item: Mapping[str, Any]) -> bool:
    reason = _mapping(item.get("reason"))
    return bool(reason and reason.get("$type") == "app.bsky.feed.defs#reasonRepost")


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _active_label_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    labels = []
    for entry in value:
        label = _mapping(entry)
        if label is None or label.get("neg") is True:
            continue
        name = _string(label.get("val"))
        if name:
            labels.append(name.strip().lower())
    return _dedupe(labels)


def _self_label_values(value: object) -> list[str]:
    labels = _mapping(value)
    if labels is None:
        return []
    return _active_label_values(labels.get("values"))


def _contains_term(normalized_text: str, term: str) -> bool:
    if re.fullmatch(r"[a-z0-9]+", term):
        return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized_text) is not None
    return term in normalized_text


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
