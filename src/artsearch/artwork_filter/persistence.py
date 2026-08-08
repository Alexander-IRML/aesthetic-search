from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol, Sequence
import uuid

from artsearch.artwork_filter.errors import PersistenceError
from artsearch.artwork_filter.schemas import FilterResult


class DecisionStore(Protocol):
    def append(self, result: FilterResult) -> None: ...

    def append_many(self, results: Sequence[FilterResult]) -> None: ...


class JSONLDecisionStore:
    def __init__(self, path: str | Path, *, append: bool = True) -> None:
        self.path = Path(path)
        self._append = append
        self._rows_written = 0
        self._working_path = (
            self.path
            if append
            else self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        )

    def append(self, result: FilterResult) -> None:
        self.append_many([result])

    def append_many(self, results: Sequence[FilterResult]) -> None:
        if not results:
            return
        try:
            self._working_path.parent.mkdir(parents=True, exist_ok=True)
            payload = "".join(
                json.dumps(result.model_dump(mode="json"), sort_keys=True) + "\n"
                for result in results
            )
            with self._working_path.open("a", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._rows_written += len(results)
            self.commit()
        except OSError as exc:
            raise PersistenceError(str(exc)) from exc

    def commit(self, *, allow_empty: bool = False) -> bool:
        """Publish staged overwrite output only after decisions were produced."""

        if self._append:
            return True
        if self._rows_written == 0 and not allow_empty:
            self.abort()
            return False
        try:
            if not self._working_path.exists():
                self._working_path.parent.mkdir(parents=True, exist_ok=True)
                self._working_path.touch()
            os.replace(self._working_path, self.path)
            _fsync_directory(self.path.parent)
        except OSError as exc:
            raise PersistenceError(str(exc)) from exc
        self._working_path = self.path
        self._append = True
        return True

    def abort(self) -> None:
        if not self._append:
            try:
                self._working_path.unlink(missing_ok=True)
            except OSError:
                pass


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
