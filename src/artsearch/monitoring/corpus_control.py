from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from threading import Lock, Thread
import time
from typing import Any
import uuid
from urllib.parse import urlparse

from artsearch.bluesky.state import SQLiteBlueskyIntakeStateStore
from artsearch.discovery.repository import DiscoveryRepository
from artsearch.ingest.config import load_config
from artsearch.postgres.database import build_engine
from artsearch.production.config import load_production_config


CommandRunner = Callable[[Sequence[str], Path, Path, Path], int]


class CorpusControlConflictError(RuntimeError):
    """Raised when an operator starts a job while another one is active."""


@dataclass(frozen=True, slots=True)
class CorpusInventory:
    payload: dict[str, Any]
    actors: tuple[str, ...]
    high_water_posts: dict[str, str]


class CorpusControlService:
    """Local-only operator controls over canonical intake and discovery jobs."""

    def __init__(
        self,
        *,
        app_config_path: Path,
        filter_config_path: Path,
        prompt_config_path: Path,
        production_config_path: Path,
        bluesky_config_path: Path = Path("configs/bluesky.default.toml"),
        discovery_config_path: Path = Path("configs/discovery.default.toml"),
        on_write: Callable[[], None] | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.app_config_path = Path(app_config_path)
        self.app = load_config(self.app_config_path)
        self.filter_config_path = self._resolve(filter_config_path)
        self.prompt_config_path = self._resolve(prompt_config_path)
        self.production_config_path = self._resolve(production_config_path)
        self.bluesky_config_path = self._resolve(bluesky_config_path)
        self.discovery_config_path = self._resolve(discovery_config_path)
        self.control_root = self.app.root_dir / "data/control"
        self.jobs_root = self.control_root / "jobs"
        self.on_write = on_write
        self.command_runner = command_runner or _run_command
        self._lock = Lock()
        self._admission_lock = Lock()
        self._job: dict[str, Any] | None = self._recover_latest_job()

    def graph(self) -> dict[str, Any]:
        inventory = self._inventory()
        payload = inventory.payload
        payload["edges"] = []
        payload["discovery"] = self._discovery_graph(payload)
        payload["counts"]["edges"] = len(payload["edges"])
        payload["controls"] = {
            "enabled": True,
            "can_refresh": bool(inventory.actors),
            "can_expand": (bool(inventory.actors) and payload["discovery"]["status"] == "online"),
        }
        return payload

    def status(self) -> dict[str, Any]:
        with self._lock:
            job = dict(self._job) if self._job is not None else None
        if job is not None and job.get("state") in {"queued", "running"}:
            started = _parse_datetime(job.get("started_at") or job.get("created_at"))
            if started is not None:
                job["elapsed_seconds"] = round(
                    max(0.0, (datetime.now(timezone.utc) - started).total_seconds()),
                    1,
                )
        return {"active": bool(job and job.get("state") in {"queued", "running"}), "job": job}

    def start_refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
        max_new_images = _bounded_int(payload, "max_new_images", 500, 1, 5000)
        max_pages = _bounded_int(payload, "max_pages_per_artist", 2, 1, 20)
        max_per_artist = _bounded_int(payload, "max_images_per_artist", 20, 1, 100)
        actor_limit = _bounded_int(payload, "artist_limit", 5000, 1, 5000)
        inventory = self._inventory()
        actors = list(inventory.actors[:actor_limit])
        if not actors:
            raise ValueError("the local corpus has no Bluesky artist identities to refresh")

        parameters = {
            "artists": len(actors),
            "max_new_images": max_new_images,
            "max_pages_per_artist": max_pages,
            "max_images_per_artist": max_per_artist,
        }
        with self._admission_lock:
            self._ensure_job_slot()
            # Existing canonical post URIs become the first high-water marks. This
            # makes the first dashboard-triggered run incremental even when older
            # CLI runs predate the intake-state table.
            SQLiteBlueskyIntakeStateStore(self.app.database_path).seed_high_water(
                {
                    actor: inventory.high_water_posts[actor]
                    for actor in actors
                    if actor in inventory.high_water_posts
                }
            )
            job_id, job_dir = self._new_job("refresh")
            roster = job_dir / "actors.txt"
            roster.write_text("".join(f"{actor}\n" for actor in actors), encoding="utf-8")
            command = [
                self._entry_point("artsearch-bluesky-pipeline"),
                "run-authors",
                "--actors-file",
                str(roster),
                "--app-config",
                str(self.app_config_path.resolve()),
                "--bluesky-config",
                str(self.bluesky_config_path),
                "--filter-config",
                str(self.filter_config_path),
                "--prompt-config",
                str(self.prompt_config_path),
                "--checkpoint",
                str(job_dir / "checkpoint.jsonl"),
                "--append-candidates",
                "--resume",
                "--no-review-download",
                "--max-pages",
                str(max_pages),
                "--max-candidates-per-actor",
                str(max_per_artist),
                "--max-candidates",
                str(max_new_images),
            ]
            return self._launch(job_id, job_dir, "refresh", command, parameters)

    def start_expand(self, payload: dict[str, Any]) -> dict[str, Any]:
        depth = _bounded_int(payload, "depth", 1, 1, 3)
        request_budget = _bounded_int(payload, "request_budget", 100, 1, 2000)
        production = load_production_config(self.production_config_path)
        if not production.postgres.enabled:
            raise ValueError(
                "creator graph expansion requires a PostgreSQL-enabled production config"
            )
        inventory = self._inventory()
        dids = [item["did"] for item in inventory.payload["artists"] if item.get("did")]
        if not dids:
            raise ValueError("the local corpus has no Bluesky DIDs to expand")

        with self._admission_lock:
            self._ensure_job_slot()
            requests_per_seed = 1 + 2
            seed_count = min(len(dids), max(1, request_budget // requests_per_seed), 250)
            seeds = self._rotate_seeds(dids, seed_count)
            creator_budget = min(2500, max(seed_count, request_budget * 5))
            media_budget = min(5000, max(25, request_budget * 10))
            max_tasks = min(5000, max(request_budget * 2, seed_count * 2))
            job_id, job_dir = self._new_job("expand")
            roster = job_dir / "seeds.txt"
            roster.write_text("".join(f"{did}\n" for did in seeds), encoding="utf-8")
            command = [
                self._entry_point("artsearch-discovery"),
                "run",
                "--production-config",
                str(self.production_config_path),
                "--discovery-config",
                str(self.discovery_config_path),
                "--bluesky-config",
                str(self.bluesky_config_path),
                "--filter-config",
                str(self.filter_config_path),
                "--prompt-config",
                str(self.prompt_config_path),
                "--app-config",
                str(self.app_config_path.resolve()),
                "--actors-file",
                str(roster),
                "--campaign-name",
                f"dashboard-expand-{job_id}",
                "--trusted-seeds",
                "--manual-seeds-only",
                "--max-depth",
                str(depth),
                "--request-budget",
                str(request_budget),
                "--creator-budget",
                str(creator_budget),
                "--media-budget",
                str(media_budget),
                "--max-tasks",
                str(max_tasks),
            ]
            parameters = {
                "depth": depth,
                "request_budget": request_budget,
                "seed_artists": seed_count,
                "creator_cap": creator_budget,
                "media_cap": media_budget,
                "task_cap": max_tasks,
            }
            return self._launch(job_id, job_dir, "expand", command, parameters)

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.status()["active"]:
                return True
            time.sleep(0.01)
        return not self.status()["active"]

    def _inventory(self) -> CorpusInventory:
        if not self.app.database_path.exists():
            raise FileNotFoundError(
                f"canonical SQLite database not found: {self.app.database_path}"
            )
        connection = sqlite3.connect(self.app.database_path)
        connection.row_factory = sqlite3.Row
        try:
            artist_rows = connection.execute(
                """
                SELECT a.artist_id, a.display_name, a.source_url,
                       COUNT(aw.artwork_id) AS artwork_count
                  FROM artists a
                  JOIN artworks aw ON aw.artist_id = a.artist_id
                 WHERE a.source_platform = 'bluesky' OR aw.source_platform = 'bluesky'
                 GROUP BY a.artist_id, a.display_name, a.source_url
                 ORDER BY a.artist_id
                """
            ).fetchall()
            artwork_artist = {
                str(row["artwork_id"]): str(row["artist_id"])
                for row in connection.execute("SELECT artwork_id, artist_id FROM artworks")
            }
            identity_rows = connection.execute(
                """
                SELECT r.artwork_id, d.author_did, d.candidate_json, d.processed_at
                  FROM artwork_filter_routes r
                  JOIN artwork_filter_decisions d ON d.decision_key = r.decision_key
                 WHERE r.artwork_id IS NOT NULL AND d.author_did IS NOT NULL
                 ORDER BY d.processed_at DESC
                """
            ).fetchall()
            decision_rows = connection.execute(
                """
                SELECT author_did, post_uri, candidate_id, decision,
                       candidate_json, processed_at
                  FROM artwork_filter_decisions
                 WHERE author_did IS NOT NULL AND post_uri IS NOT NULL
                 ORDER BY processed_at DESC
                """
            ).fetchall()
        finally:
            connection.close()

        identities: dict[str, dict[str, str]] = {}
        for row in identity_rows:
            artist_id = artwork_artist.get(str(row["artwork_id"]))
            if not artist_id or artist_id in identities:
                continue
            candidate = _json_object(row["candidate_json"])
            identities[artist_id] = {
                "did": str(row["author_did"]),
                "handle": str(candidate.get("author_handle") or ""),
            }

        artists: list[dict[str, Any]] = []
        did_to_index: dict[str, int] = {}
        actor_to_index: dict[str, int] = {}
        for row in artist_rows:
            artist_id = str(row["artist_id"])
            identity = identities.get(artist_id, {})
            did = identity.get("did", "")
            handle = identity.get("handle", "") or _handle_from_profile_url(row["source_url"])
            actor = did or handle
            entry = {
                "key": _compact_key("artist", did or artist_id),
                "artist_id": artist_id,
                "did": did or None,
                "handle": handle or None,
                "display_name": str(row["display_name"]),
                "artworks": int(row["artwork_count"]),
                "posts": 0,
                "last_pulled_at": None,
                "in_corpus": True,
                "state": "corpus",
                "safety_state": None,
                "depth": 0,
            }
            index = len(artists)
            artists.append(entry)
            if did:
                did_to_index.setdefault(did, index)
            if actor:
                actor_to_index.setdefault(actor, index)

        post_state: dict[tuple[int, str], dict[str, Any]] = {}
        latest_by_actor: dict[str, tuple[str, str]] = {}
        decision_bits = {"accept": 1, "review": 2, "reject": 4, "error": 8}
        for row in decision_rows:
            did = str(row["author_did"])
            artist_index = did_to_index.get(did)
            if artist_index is None:
                continue
            post_uri = str(row["post_uri"])
            key = (artist_index, post_uri)
            state = post_state.setdefault(
                key,
                {
                    "candidates": set(),
                    "decision_mask": 0,
                    "latest_at": "",
                },
            )
            state["candidates"].add(str(row["candidate_id"]))
            state["decision_mask"] |= decision_bits.get(str(row["decision"]), 0)
            candidate = _json_object(row["candidate_json"])
            observed_at = str(candidate.get("created_at") or row["processed_at"] or "")
            state["latest_at"] = max(state["latest_at"], observed_at)
            current = latest_by_actor.get(did)
            if current is None or observed_at > current[0]:
                latest_by_actor[did] = (observed_at, post_uri)

        posts: list[list[Any]] = []
        posts_by_artist: dict[int, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for (artist_index, post_uri), state in post_state.items():
            posts_by_artist[artist_index].append((post_uri, state))
        for artist_index in range(len(artists)):
            values = sorted(
                posts_by_artist.get(artist_index, ()),
                key=lambda item: (item[1]["latest_at"], item[0]),
                reverse=True,
            )
            artists[artist_index]["posts"] = len(values)
            if values:
                artists[artist_index]["last_pulled_at"] = values[0][1]["latest_at"] or None
            for post_uri, state in values:
                # Array layout keeps 10k+ post identities small on disk and over localhost:
                # [artist index, compact post key, image candidate count, decision bit mask].
                posts.append(
                    [
                        artist_index,
                        _compact_key("post", post_uri),
                        len(state["candidates"]),
                        state["decision_mask"],
                    ]
                )

        actors = tuple(
            actor for actor, _ in sorted(actor_to_index.items(), key=lambda item: item[1])
        )
        high_water = {
            actor: latest_by_actor[actor][1] for actor in actors if actor in latest_by_actor
        }
        return CorpusInventory(
            payload={
                "schema_version": "1.0",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "counts": {
                    "artists": len(artists),
                    "corpus_artists": len(artists),
                    "discovered_artists": 0,
                    "posts": len(posts),
                    "artworks": sum(item["artworks"] for item in artists),
                    "edges": 0,
                },
                "artists": artists,
                "posts": posts,
            },
            actors=actors,
            high_water_posts=high_water,
        )

    def _discovery_graph(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            production = load_production_config(self.production_config_path)
        except (OSError, ValueError) as exc:
            return {"configured": False, "status": "error", "message": str(exc)}
        if not production.postgres.enabled:
            return {
                "configured": False,
                "status": "disabled",
                "message": "PostgreSQL discovery control plane is disabled.",
            }
        engine = None
        try:
            engine = build_engine(production.postgres)
            repository = DiscoveryRepository(engine)
            did_to_index = {
                item["did"]: index
                for index, item in enumerate(payload["artists"])
                if item.get("did")
            }
            for row in repository.creator_graph_nodes():
                did = str(row["did"])
                if did in did_to_index:
                    item = payload["artists"][did_to_index[did]]
                    item["state"] = str(row["state"])
                    item["safety_state"] = str(row["safety_state"])
                    item["depth"] = int(row["minimum_depth"])
                    continue
                did_to_index[did] = len(payload["artists"])
                payload["artists"].append(
                    {
                        "key": _compact_key("artist", did),
                        "artist_id": None,
                        "did": did,
                        "handle": row["handle"],
                        "display_name": row["display_name"] or row["handle"] or did,
                        "artworks": 0,
                        "posts": 0,
                        "last_pulled_at": None,
                        "in_corpus": False,
                        "state": str(row["state"]),
                        "safety_state": str(row["safety_state"]),
                        "depth": int(row["minimum_depth"]),
                    }
                )
            payload["counts"]["artists"] = len(payload["artists"])
            payload["counts"]["discovered_artists"] = (
                len(payload["artists"]) - payload["counts"]["corpus_artists"]
            )
            rows = repository.creator_graph_edges(tuple(did_to_index))
            for row in rows:
                source = did_to_index.get(str(row["source_did"]))
                target = did_to_index.get(str(row["target_did"]))
                if source is None or target is None:
                    continue
                payload["edges"].append(
                    [
                        source,
                        target,
                        str(row["edge_type"]),
                        round(float(row["confidence"]), 4),
                        int(row["support_count"]),
                    ]
                )
            return {"configured": True, "status": "online", "message": None}
        except Exception as exc:
            return {
                "configured": True,
                "status": "unavailable",
                "message": f"{type(exc).__name__}: {exc}",
            }
        finally:
            if engine is not None:
                engine.dispose()

    def _new_job(self, kind: str) -> tuple[str, Path]:
        job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        job_dir = self.jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        return job_id, job_dir

    def _ensure_job_slot(self) -> None:
        with self._lock:
            if self._job is not None and self._job.get("state") in {"queued", "running"}:
                raise CorpusControlConflictError(
                    f"job {self._job['job_id']} is already {self._job['state']}"
                )

    def _launch(
        self,
        job_id: str,
        job_dir: Path,
        kind: str,
        command: list[str],
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        job = {
            "job_id": job_id,
            "kind": kind,
            "state": "queued",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "parameters": parameters,
            "summary": None,
            "exit_code": None,
            "message": "Waiting for the background worker.",
        }
        with self._lock:
            if self._job is not None and self._job.get("state") in {"queued", "running"}:
                raise CorpusControlConflictError(
                    f"job {self._job['job_id']} is already {self._job['state']}"
                )
            self._job = job
            self._write_job(job_dir, job)
        Thread(
            target=self._execute_job,
            args=(job_dir, command),
            name=f"artsearch-{kind}-{job_id}",
            daemon=True,
        ).start()
        return self.status()

    def _execute_job(self, job_dir: Path, command: list[str]) -> None:
        self._update_job(
            job_dir,
            state="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            message="Background command is running.",
        )
        stdout_path = job_dir / "stdout.json"
        stderr_path = job_dir / "job.log"
        try:
            exit_code = self.command_runner(
                command,
                self.app.root_dir,
                stdout_path,
                stderr_path,
            )
            summary = _read_command_summary(stdout_path)
            state = "succeeded" if exit_code == 0 else "failed"
            message = (
                "Job completed successfully."
                if exit_code == 0
                else _tail(stderr_path) or f"Background command exited with code {exit_code}."
            )
            self._update_job(
                job_dir,
                state=state,
                finished_at=datetime.now(timezone.utc).isoformat(),
                exit_code=exit_code,
                summary=summary,
                message=message,
            )
        except Exception as exc:
            self._update_job(
                job_dir,
                state="failed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                exit_code=None,
                summary=None,
                message=f"{type(exc).__name__}: {exc}",
            )
        if self.on_write is not None:
            self.on_write()

    def _update_job(self, job_dir: Path, **updates: Any) -> None:
        with self._lock:
            if self._job is None:
                return
            self._job = {**self._job, **updates}
            self._write_job(job_dir, self._job)

    def _write_job(self, job_dir: Path, job: dict[str, Any]) -> None:
        destination = job_dir / "status.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(job, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)

    def _recover_latest_job(self) -> dict[str, Any] | None:
        if not self.jobs_root.exists():
            return None
        paths = sorted(self.jobs_root.glob("*/status.json"), reverse=True)
        if not paths:
            return None
        try:
            value = json.loads(paths[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        if value.get("state") in {"queued", "running"}:
            value.update(
                state="interrupted",
                finished_at=datetime.now(timezone.utc).isoformat(),
                message="Dashboard restarted before this job recorded completion.",
            )
            self._write_job(paths[0].parent, value)
        return value

    def _rotate_seeds(self, dids: list[str], count: int) -> list[str]:
        path = self.control_root / "discovery-seed-cursor.json"
        start = 0
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            start = int(value.get("next", 0))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            pass
        start %= len(dids)
        ordered = dids[start:] + dids[:start]
        selected = ordered[:count]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"next": (start + len(selected)) % len(dids)}) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return selected

    def _entry_point(self, name: str) -> str:
        sibling = Path(sys.executable).parent / name
        if sibling.is_file():
            return str(sibling)
        project = self.app.root_dir / ".venv/bin" / name
        if project.is_file():
            return str(project)
        located = shutil.which(name)
        if located:
            return located
        raise FileNotFoundError(f"required executable is not installed: {name}")

    def _resolve(self, path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (self.app.root_dir / path).resolve()


def _run_command(
    command: Sequence[str],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> int:
    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stderr_path.open("w", encoding="utf-8") as stderr,
    ):
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    return int(completed.returncode)


def _compact_key(namespace: str, value: str) -> str:
    digest = hashlib.blake2s(value.encode("utf-8"), digest_size=8).hexdigest()
    return f"{namespace[0]}:{digest}"


def _bounded_int(
    payload: dict[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = payload.get(name, default)
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _handle_from_profile_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    try:
        index = parts.index("profile")
    except ValueError:
        return ""
    return parts[index + 1] if index + 1 < len(parts) else ""


def _read_command_summary(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if len(text) > 1_000_000:
        text = text[-1_000_000:]
    try:
        value = json.loads(text)
    except ValueError:
        for line in reversed(text.splitlines()):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
        return None
    return value if isinstance(value, dict) else None


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
