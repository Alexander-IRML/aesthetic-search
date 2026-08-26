from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from artsearch.artwork_filter.hashing import stable_json_hash
from artsearch.ingest.db import connect, init_db


@dataclass(frozen=True)
class ActorIntakeState:
    actor: str
    high_water_post_uri: str | None = None
    pending_high_water_post_uri: str | None = None
    resume_cursor: str | None = None
    last_scan_complete: bool = False
    last_candidate_count: int = 0
    last_error: str | None = None
    last_attempt_at: str | None = None
    last_completed_at: str | None = None


class SQLiteBlueskyIntakeStateStore:
    """Persist incremental actor scans and fair roster rotation in canonical SQLite."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        with connect(self.database_path) as connection:
            init_db(connection)
            connection.commit()

    def actor_state(self, actor: str) -> ActorIntakeState:
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT actor, high_water_post_uri, pending_high_water_post_uri,
                       resume_cursor, last_scan_complete,
                       last_candidate_count, last_error, last_attempt_at,
                       last_completed_at
                  FROM bluesky_actor_intake_state
                 WHERE actor_key = ?
                """,
                (_actor_key(actor),),
            ).fetchone()
        if row is None:
            return ActorIntakeState(actor=actor)
        return ActorIntakeState(
            actor=str(row["actor"]),
            high_water_post_uri=row["high_water_post_uri"],
            pending_high_water_post_uri=row["pending_high_water_post_uri"],
            resume_cursor=row["resume_cursor"],
            last_scan_complete=bool(row["last_scan_complete"]),
            last_candidate_count=int(row["last_candidate_count"]),
            last_error=row["last_error"],
            last_attempt_at=row["last_attempt_at"],
            last_completed_at=row["last_completed_at"],
        )

    def record_scan(
        self,
        actor: str,
        *,
        newest_post_uri: str | None,
        candidate_count: int,
        scan_complete: bool,
        resume_cursor: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        current = self.actor_state(actor)
        high_water = (
            newest_post_uri
            if scan_complete and newest_post_uri is not None
            else current.high_water_post_uri
        )
        pending_high_water = (
            None if scan_complete else current.pending_high_water_post_uri or newest_post_uri
        )
        stored_cursor = None if scan_complete else resume_cursor
        completed_at = now if scan_complete else current.last_completed_at
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO bluesky_actor_intake_state (
                    actor_key, actor, high_water_post_uri,
                    pending_high_water_post_uri, resume_cursor, last_scan_complete,
                    last_candidate_count, last_error, last_attempt_at,
                    last_completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(actor_key) DO UPDATE SET
                    actor = excluded.actor,
                    high_water_post_uri = excluded.high_water_post_uri,
                    pending_high_water_post_uri = excluded.pending_high_water_post_uri,
                    resume_cursor = excluded.resume_cursor,
                    last_scan_complete = excluded.last_scan_complete,
                    last_candidate_count = excluded.last_candidate_count,
                    last_error = NULL,
                    last_attempt_at = excluded.last_attempt_at,
                    last_completed_at = excluded.last_completed_at
                """,
                (
                    _actor_key(actor),
                    actor,
                    high_water,
                    pending_high_water,
                    stored_cursor,
                    int(scan_complete),
                    candidate_count,
                    now,
                    completed_at,
                ),
            )
            connection.commit()

    def record_error(self, actor: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        current = self.actor_state(actor)
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO bluesky_actor_intake_state (
                    actor_key, actor, high_water_post_uri,
                    pending_high_water_post_uri, resume_cursor, last_scan_complete,
                    last_candidate_count, last_error, last_attempt_at,
                    last_completed_at
                ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                ON CONFLICT(actor_key) DO UPDATE SET
                    actor = excluded.actor,
                    last_scan_complete = 0,
                    last_error = excluded.last_error,
                    last_attempt_at = excluded.last_attempt_at
                """,
                (
                    _actor_key(actor),
                    actor,
                    current.high_water_post_uri,
                    current.pending_high_water_post_uri,
                    current.resume_cursor,
                    error[:2000],
                    now,
                    current.last_completed_at,
                ),
            )
            connection.commit()

    def roster_start(self, actors: list[str]) -> tuple[str, int]:
        if not actors:
            raise ValueError("actor roster must not be empty")
        key = roster_key(actors)
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT next_actor_index
                  FROM bluesky_roster_intake_state
                 WHERE roster_key = ? AND actor_count = ?
                """,
                (key, len(actors)),
            ).fetchone()
        return key, int(row["next_actor_index"]) % len(actors) if row is not None else 0

    def advance_roster(self, roster_key_value: str, actor_count: int, next_index: int) -> None:
        if actor_count <= 0:
            raise ValueError("actor_count must be positive")
        now = datetime.now(timezone.utc).isoformat()
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO bluesky_roster_intake_state (
                    roster_key, actor_count, next_actor_index, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(roster_key) DO UPDATE SET
                    actor_count = excluded.actor_count,
                    next_actor_index = excluded.next_actor_index,
                    updated_at = excluded.updated_at
                """,
                (roster_key_value, actor_count, next_index % actor_count, now),
            )
            connection.commit()


def roster_key(actors: list[str]) -> str:
    return stable_json_hash(
        {
            "version": "bluesky-roster-v1",
            "actors": [actor.casefold() for actor in actors],
        }
    )


def rotated_roster(actors: list[str], start_index: int) -> list[tuple[int, str]]:
    if not actors:
        return []
    start = start_index % len(actors)
    indexed = list(enumerate(actors))
    return [*indexed[start:], *indexed[:start]]


def _actor_key(actor: str) -> str:
    return stable_json_hash({"actor": actor.casefold()})
