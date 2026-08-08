from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Iterable
import uuid

from artsearch.artwork_filter.hashing import stable_json_hash
from artsearch.artwork_filter.schemas import ImageCandidate


class JSONLCandidateStore:
    """Append candidates durably or atomically replace a prior candidate stream."""

    def __init__(self, path: str | Path, *, append: bool = True) -> None:
        self.path = Path(path)
        self._append = append
        self._rows_written = 0
        self._working_path = (
            self.path
            if append
            else self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        )
        self._seen_versions = self._load_seen_versions() if append else set()

    @property
    def rows_written(self) -> int:
        return self._rows_written

    def append_many(self, candidates: Iterable[ImageCandidate]) -> int:
        self._working_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with self._working_path.open("a", encoding="utf-8") as handle:
            for candidate in candidates:
                version = (candidate.candidate_id, candidate.post_cid)
                if version in self._seen_versions:
                    continue
                handle.write(json.dumps(candidate.model_dump(mode="json"), sort_keys=True) + "\n")
                self._seen_versions.add(version)
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        self._rows_written += count
        return count

    def commit(self, *, allow_empty: bool = False) -> bool:
        """Publish a staged replacement after a successful collection boundary."""

        if self._append:
            return True
        if self._rows_written == 0 and not allow_empty:
            self.abort()
            return False
        if not self._working_path.exists():
            self._working_path.parent.mkdir(parents=True, exist_ok=True)
            self._working_path.touch()
        os.replace(self._working_path, self.path)
        _fsync_directory(self.path.parent)
        self._working_path = self.path
        self._append = True
        return True

    def abort(self) -> None:
        if not self._append:
            self._working_path.unlink(missing_ok=True)

    def _load_seen_versions(self) -> set[tuple[str, str | None]]:
        if not self.path.exists():
            return set()
        seen: set[tuple[str, str | None]] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                candidate_id = payload.get("candidate_id") if isinstance(payload, dict) else None
                post_cid = payload.get("post_cid") if isinstance(payload, dict) else None
                if isinstance(candidate_id, str) and (
                    post_cid is None or isinstance(post_cid, str)
                ):
                    seen.add((candidate_id, post_cid))
        return seen


class JSONLActorCheckpointStore:
    """Record successful actor collections without conflating different run settings."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._completed = self._load_completed()

    def is_completed(self, key: str) -> bool:
        return key in self._completed

    def mark_completed(
        self,
        *,
        key: str,
        actor: str,
        candidate_count: int,
        settings: dict[str, object],
    ) -> None:
        if key in self._completed:
            return
        payload = {
            "checkpoint_key": key,
            "actor": actor,
            "candidate_count": candidate_count,
            "settings": settings,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._completed.add(key)

    def _load_completed(self) -> set[str]:
        if not self.path.exists():
            return set()
        completed: set[str] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = payload.get("checkpoint_key") if isinstance(payload, dict) else None
                if isinstance(key, str):
                    completed.add(key)
        return completed


def actor_checkpoint_key(actor: str, settings: dict[str, object]) -> str:
    return stable_json_hash(
        {
            "actor": actor.casefold(),
            "settings": settings,
        }
    )


def write_candidates_jsonl(
    path: str | Path,
    candidates: Iterable[ImageCandidate],
    *,
    append: bool = False,
) -> int:
    store = JSONLCandidateStore(path, append=append)
    try:
        count = store.append_many(candidates)
        store.commit(allow_empty=True)
        return count
    except OSError:
        store.abort()
        raise


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
