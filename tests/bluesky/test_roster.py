from pathlib import Path

import pytest

from artsearch.bluesky.roster import normalize_actor, read_actor_roster


def test_text_roster_preserves_order_and_deduplicates(tmp_path: Path) -> None:
    roster = tmp_path / "actors.txt"
    roster.write_text(
        "# private roster\n@one.bsky.social\nDID:PLC:ABC\none.bsky.social\n",
        encoding="utf-8",
    )

    assert read_actor_roster(roster) == ["one.bsky.social", "DID:PLC:ABC"]


def test_csv_roster_accepts_handles_dids_and_profile_urls(tmp_path: Path) -> None:
    roster = tmp_path / "actors.csv"
    roster.write_text(
        "artist name,Bluesky Handle,profile_url\n"
        "One,@one.bsky.social,\n"
        "Two,,https://bsky.app/profile/did%3Aplc%3Atwo/post/ignored\n"
        "Duplicate,ONE.BSKY.SOCIAL,\n",
        encoding="utf-8",
    )

    assert read_actor_roster(roster) == ["one.bsky.social", "did:plc:two"]


def test_csv_roster_rejects_unknown_columns(tmp_path: Path) -> None:
    roster = tmp_path / "actors.csv"
    roster.write_text("artist,website\nOne,https://example.com\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no supported identity column"):
        read_actor_roster(roster)


def test_actor_normalization_rejects_non_profile_urls() -> None:
    with pytest.raises(ValueError, match="unsupported Bluesky profile URL"):
        normalize_actor("https://bsky.app/search?q=artist")
