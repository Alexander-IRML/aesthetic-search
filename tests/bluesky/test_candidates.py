from artsearch.artwork_filter.hashing import make_candidate_id
from artsearch.bluesky.candidates import candidates_from_feed_item
from artsearch.bluesky.config import BlueskyModerationConfig


def test_candidates_from_image_embed_extracts_stable_bluesky_fields():
    item = {
        "post": {
            "uri": "at://did:plc:artist/app.bsky.feed.post/post1",
            "cid": "bafy-post",
            "author": {
                "did": "did:plc:artist",
                "handle": "artist.example",
            },
            "record": {
                "text": "finished drawing",
                "createdAt": "2026-07-16T12:30:00.000Z",
                "langs": ["en"],
            },
            "embed": {
                "$type": "app.bsky.embed.images#view",
                "images": [
                    {
                        "thumb": "https://cdn.example/thumb.jpg",
                        "fullsize": "https://cdn.example/full.jpg",
                        "alt": "character illustration",
                        "aspectRatio": {"width": 1280, "height": 720},
                    }
                ],
            },
        }
    }

    candidates = candidates_from_feed_item(item)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.candidate_id == make_candidate_id(item["post"]["uri"], 0)
    assert candidate.author_did == "did:plc:artist"
    assert candidate.author_handle == "artist.example"
    assert candidate.post_uri == item["post"]["uri"]
    assert candidate.post_cid == "bafy-post"
    assert candidate.image_index == 0
    assert candidate.thumbnail_url == "https://cdn.example/thumb.jpg"
    assert candidate.fullsize_url == "https://cdn.example/full.jpg"
    assert candidate.post_text == "finished drawing"
    assert candidate.alt_text == "character illustration"
    assert candidate.created_at.year == 2026
    assert candidate.langs == ["en"]
    assert candidate.declared_width == 1280
    assert candidate.declared_height == 720
    assert candidate.source == "bluesky"


def test_candidates_from_record_with_media_marks_quote_and_repost():
    item = {
        "reason": {"$type": "app.bsky.feed.defs#reasonRepost"},
        "post": {
            "uri": "at://did:plc:artist/app.bsky.feed.post/post2",
            "author": {"did": "did:plc:artist", "handle": "artist.example"},
            "record": {"text": "quoted art"},
            "embed": {
                "$type": "app.bsky.embed.recordWithMedia#view",
                "record": {
                    "author": {
                        "did": "did:plc:quoted",
                        "handle": "quoted.example",
                    }
                },
                "media": {
                    "$type": "app.bsky.embed.images#view",
                    "images": [
                        {
                            "thumb": "https://cdn.example/q-thumb.jpg",
                            "fullsize": "https://cdn.example/q-full.jpg",
                            "alt": "",
                        }
                    ],
                },
            },
        },
    }

    candidates = candidates_from_feed_item(item)

    assert len(candidates) == 1
    assert candidates[0].is_repost is True
    assert candidates[0].is_quote_post is True
    assert candidates[0].quoted_author_did == "did:plc:quoted"


def test_candidates_from_non_image_post_returns_empty_list():
    item = {
        "post": {
            "uri": "at://did:plc:artist/app.bsky.feed.post/post3",
            "author": {"did": "did:plc:artist"},
            "record": {"text": "just text"},
        }
    }

    assert candidates_from_feed_item(item) == []


def test_public_safe_moderation_excludes_labeled_adult_post():
    item = _image_item()
    item["post"]["labels"] = [
        {"val": "porn", "neg": False},
        {"val": "sexual", "neg": True},
    ]

    candidates = candidates_from_feed_item(
        item,
        moderation=BlueskyModerationConfig(),
    )

    assert candidates == []


def test_public_safe_moderation_excludes_text_signal():
    item = _image_item()
    item["post"]["record"]["text"] = "NSFW artwork"

    candidates = candidates_from_feed_item(
        item,
        moderation=BlueskyModerationConfig(),
    )

    assert candidates == []


def test_non_blocking_labels_are_preserved_for_audit():
    item = _image_item()
    item["post"]["labels"] = [{"val": "bot"}]
    item["post"]["author"]["labels"] = [{"val": "verified"}]

    candidates = candidates_from_feed_item(
        item,
        moderation=BlueskyModerationConfig(),
    )

    assert candidates[0].content_labels == ["bot"]
    assert candidates[0].author_labels == ["verified"]


def _image_item():
    return {
        "post": {
            "uri": "at://did:plc:artist/app.bsky.feed.post/safe",
            "cid": "safe-cid",
            "author": {
                "did": "did:plc:artist",
                "handle": "artist.example",
            },
            "record": {"text": "finished drawing"},
            "embed": {
                "$type": "app.bsky.embed.images#view",
                "images": [
                    {
                        "thumb": "https://cdn.example/thumb.jpg",
                        "fullsize": "https://cdn.example/full.jpg",
                        "alt": "character illustration",
                    }
                ],
            },
        }
    }
