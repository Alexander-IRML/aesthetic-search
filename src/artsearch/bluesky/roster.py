from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import unquote, urlparse


_ACTOR_COLUMNS = (
    "did",
    "bluesky_did",
    "actor",
    "handle",
    "bluesky_handle",
    "profile_url",
    "bluesky_url",
    "url",
)


def read_actor_roster(path: str | Path) -> list[str]:
    """Load an ordered, deduplicated Bluesky actor roster from text or CSV."""

    source = Path(path)
    if source.suffix.casefold() == ".csv":
        values = _read_csv_roster(source)
    else:
        values = source.read_text(encoding="utf-8-sig").splitlines()
    return _normalize_roster(values)


def _read_csv_roster(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []
        normalized_fields = {
            _normalize_column_name(name): name for name in reader.fieldnames if name is not None
        }
        actor_columns = [
            normalized_fields[name] for name in _ACTOR_COLUMNS if name in normalized_fields
        ]
        if not actor_columns:
            available = ", ".join(normalized_fields) or "none"
            expected = ", ".join(_ACTOR_COLUMNS)
            raise ValueError(
                f"actor CSV has no supported identity column; found {available}; "
                f"expected one of {expected}"
            )
        values: list[str] = []
        for row in reader:
            value = next(
                (
                    str(row.get(column, "")).strip()
                    for column in actor_columns
                    if str(row.get(column, "")).strip()
                ),
                "",
            )
            if value:
                values.append(value)
        return values


def _normalize_roster(values: list[str]) -> list[str]:
    actors: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw = value.strip()
        if not raw or raw.startswith("#"):
            continue
        actor = normalize_actor(raw)
        identity = actor.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        actors.append(actor)
    return actors


def normalize_actor(value: str) -> str:
    actor = value.strip()
    if not actor:
        return ""
    parsed = urlparse(actor)
    if parsed.scheme in {"http", "https"} and parsed.netloc.casefold() in {
        "bsky.app",
        "www.bsky.app",
    }:
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0].casefold() == "profile":
            actor = parts[1]
        else:
            raise ValueError(f"unsupported Bluesky profile URL: {value}")
    actor = actor.removeprefix("@").strip()
    if not actor or any(character.isspace() for character in actor):
        raise ValueError(f"invalid Bluesky actor identity: {value!r}")
    return actor


def _normalize_column_name(value: str) -> str:
    return value.strip().casefold().replace(" ", "_").replace("-", "_")
