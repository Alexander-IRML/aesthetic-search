from artsearch.bluesky.audit import audit_feed_items, select_safe_pilot
from artsearch.bluesky.config import BlueskyModerationConfig


def test_audit_counts_exclusions_and_subject_signals():
    items = [
        _image_item("safe-feminine", text="A heroine and her dragon"),
        _image_item("safe-mixed", text="A woman and a man adventuring"),
        _image_item("adult", labels=[{"val": "porn"}]),
    ]

    audit = audit_feed_items(
        "artist.example",
        items,
        moderation=BlueskyModerationConfig(),
    )

    assert audit.image_posts == 3
    assert audit.allowed_candidates == 2
    assert audit.excluded_image_posts == 1
    assert audit.label_excluded_posts == 1
    assert audit.feminine_signal_posts == 2
    assert audit.masculine_signal_posts == 1
    assert audit.mixed_signal_posts == 1
    assert audit.excluded_fraction == 1 / 3


def test_safe_pilot_prefers_feminine_or_mixed_and_drops_masculine_only():
    moderation = BlueskyModerationConfig()
    audits = [
        audit_feed_items(
            "feminine.example",
            [_image_item("feminine", text="A woman heroine") for _ in range(4)],
            moderation=moderation,
        ),
        audit_feed_items(
            "mixed.example",
            [_image_item("mixed", text="A woman and man") for _ in range(4)],
            moderation=moderation,
        ),
        audit_feed_items(
            "masculine.example",
            [_image_item("masculine", text="A man and his son") for _ in range(4)],
            moderation=moderation,
        ),
    ]

    selected = select_safe_pilot(
        audits,
        count=3,
        min_allowed_candidates=3,
        max_excluded_fraction=0.1,
    )

    assert selected == ["feminine.example", "mixed.example"]


def _image_item(post_id, *, text="", labels=None):
    return {
        "post": {
            "uri": f"at://did:plc:artist/app.bsky.feed.post/{post_id}",
            "cid": f"cid-{post_id}",
            "labels": labels or [],
            "author": {
                "did": "did:plc:artist",
                "handle": "artist.example",
            },
            "record": {"text": text},
            "embed": {
                "$type": "app.bsky.embed.images#view",
                "images": [
                    {
                        "thumb": f"https://cdn.example/{post_id}-thumb.jpg",
                        "fullsize": f"https://cdn.example/{post_id}-full.jpg",
                        "alt": "",
                    }
                ],
            },
        }
    }
