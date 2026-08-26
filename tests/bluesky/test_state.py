import asyncio

from artsearch.bluesky.candidates import AuthorScanProgress, iter_author_image_candidates
from artsearch.bluesky.client import AuthorFeedPage
from artsearch.bluesky.config import BlueskyModerationConfig
from artsearch.bluesky.state import SQLiteBlueskyIntakeStateStore, rotated_roster


def test_sqlite_intake_state_preserves_high_water_on_incomplete_scan(tmp_path):
    store = SQLiteBlueskyIntakeStateStore(tmp_path / "artsearch.db")
    store.record_scan(
        "artist.example",
        newest_post_uri="at://artist/post/newest",
        candidate_count=12,
        scan_complete=True,
    )
    store.record_scan(
        "artist.example",
        newest_post_uri="at://artist/post/later",
        candidate_count=100,
        scan_complete=False,
        resume_cursor="cursor-page-2",
    )

    state = store.actor_state("ARTIST.EXAMPLE")

    assert state.high_water_post_uri == "at://artist/post/newest"
    assert state.last_scan_complete is False
    assert state.last_candidate_count == 100
    assert state.pending_high_water_post_uri == "at://artist/post/later"
    assert state.resume_cursor == "cursor-page-2"


def test_sqlite_roster_rotation_resumes_after_last_actor(tmp_path):
    actors = ["one.example", "two.example", "three.example"]
    store = SQLiteBlueskyIntakeStateStore(tmp_path / "artsearch.db")
    key, start = store.roster_start(actors)
    assert start == 0

    store.advance_roster(key, len(actors), 2)
    _, resumed = store.roster_start(actors)

    assert resumed == 2
    assert rotated_roster(actors, resumed) == [
        (2, "three.example"),
        (0, "one.example"),
        (1, "two.example"),
    ]


def test_candidate_iteration_stops_at_actor_high_water():
    progress = AuthorScanProgress()
    client = _FakeClient(
        AuthorFeedPage(
            feed=[
                _item("new", "new-cid"),
                _item("old", "old-cid"),
                _item("older", "older-cid"),
            ],
            cursor="older-page",
        )
    )

    async def collect():
        return [
            candidate
            async for candidate in iter_author_image_candidates(
                client,
                "artist.example",
                max_pages=1,
                moderation=BlueskyModerationConfig(),
                stop_post_uri="at://did:plc:artist/app.bsky.feed.post/old",
                progress=progress,
            )
        ]

    candidates = asyncio.run(collect())

    assert [candidate.post_cid for candidate in candidates] == ["new-cid"]
    assert progress.newest_post_uri.endswith("/new")
    assert progress.reached_high_water is True
    assert progress.pages_seen == 1


def test_candidate_iteration_stops_at_balanced_actor_limit():
    progress = AuthorScanProgress()
    client = _FakeClient(
        AuthorFeedPage(
            feed=[
                _item("one", "one-cid"),
                _item("two", "two-cid"),
                _item("three", "three-cid"),
            ],
            cursor="older-page",
        )
    )

    async def collect():
        return [
            candidate
            async for candidate in iter_author_image_candidates(
                client,
                "artist.example",
                max_pages=1,
                max_candidates=2,
                moderation=BlueskyModerationConfig(),
                progress=progress,
            )
        ]

    candidates = asyncio.run(collect())

    assert [candidate.post_cid for candidate in candidates] == ["one-cid", "two-cid"]
    assert progress.candidate_limit_reached is True


class _FakeClient:
    def __init__(self, *pages):
        self.pages = pages

    async def iter_author_feed(self, actor, **kwargs):
        for page in self.pages:
            yield page


def _item(post_id: str, cid: str) -> dict[str, object]:
    return {
        "post": {
            "uri": f"at://did:plc:artist/app.bsky.feed.post/{post_id}",
            "cid": cid,
            "author": {"did": "did:plc:artist", "handle": "artist.example"},
            "record": {"text": "finished art"},
            "embed": {
                "$type": "app.bsky.embed.images#view",
                "images": [
                    {
                        "thumb": f"https://cdn.example/{post_id}-thumb.jpg",
                        "fullsize": f"https://cdn.example/{post_id}.jpg",
                        "alt": "illustration",
                    }
                ],
            },
        }
    }
