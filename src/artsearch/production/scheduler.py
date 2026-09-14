from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, time, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time as time_module
import tomllib
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator, model_validator

from artsearch.bluesky.roster import read_actor_roster


DEFAULT_CONFIG = "configs/intake_scheduler.default.toml"
TERMINAL_DAG_STATES = {"success", "failed"}
ACTIVE_DAG_STATES = {"queued", "running", "success", "failed"}
DOCKER_DESKTOP_WINDOWS_CLI = Path("/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe")


class IntakeSchedulerConfig(BaseModel):
    version: str = "1.0.0"
    task_name: str = "ArtSearch Daily Intake"
    timezone: str = "America/Chicago"
    daily_time: str = "10:00"
    latest_start_minutes: int = 15
    require_ac_power: bool = True
    candidate_target: int = 5000
    candidates_per_actor: int = 20
    dag_id: str = "artsearch_bluesky_intake"
    compose_file: Path = Path("orchestration/airflow/compose.yaml")
    actors_file: Path = Path("config/bluesky_actors.local.txt")
    filter_config: Path = Path("configs/artwork_filter.default.toml")
    production_config: Path = Path("configs/production.default.toml")
    discovery_enabled: bool = False
    discovery_config: Path = Path("configs/discovery.default.toml")
    discovery_creator_budget: int = 200
    discovery_max_tasks: int = 500
    minimum_free_gb: int = 25
    startup_timeout_seconds: int = 600
    run_timeout_seconds: int = 50_400
    poll_seconds: int = 30
    restart_count: int = 2
    restart_interval_minutes: int = 5
    config_hash: str = ""

    @field_validator("version", "task_name", "timezone", "dag_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("scheduler names and versions must not be empty")
        return normalized

    @field_validator("daily_time")
    @classmethod
    def _valid_daily_time(cls, value: str) -> str:
        _parse_daily_time(value)
        return value

    @model_validator(mode="after")
    def _positive_limits(self) -> "IntakeSchedulerConfig":
        positive = {
            "latest_start_minutes": self.latest_start_minutes,
            "candidate_target": self.candidate_target,
            "candidates_per_actor": self.candidates_per_actor,
            "discovery_creator_budget": self.discovery_creator_budget,
            "discovery_max_tasks": self.discovery_max_tasks,
            "minimum_free_gb": self.minimum_free_gb,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "run_timeout_seconds": self.run_timeout_seconds,
            "poll_seconds": self.poll_seconds,
            "restart_interval_minutes": self.restart_interval_minutes,
        }
        if any(value <= 0 for value in positive.values()):
            raise ValueError("scheduler limits must be positive")
        if self.restart_count < 0:
            raise ValueError("restart_count must be non-negative")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown scheduler timezone: {self.timezone}") from exc
        return self


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run or install the local ArtSearch daily Airflow intake trigger."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run-due", help="Run only inside today's configured start window.")
    subparsers.add_parser("run-now", help="Trigger and monitor one intake immediately.")
    subparsers.add_parser("install-windows", help="Install the daily Windows scheduled task.")
    subparsers.add_parser("uninstall-windows", help="Remove the Windows scheduled task.")
    subparsers.add_parser("status", help="Print scheduler configuration and local preflight.")
    args = parser.parse_args()

    try:
        if args.command == "run-due":
            report = run_intake(args.config, due_only=True)
        elif args.command == "run-now":
            report = run_intake(args.config, due_only=False)
        elif args.command == "install-windows":
            report = install_windows_task(args.config)
        elif args.command == "uninstall-windows":
            report = uninstall_windows_task(args.config)
        else:
            report = scheduler_status(args.config)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(report, sort_keys=True))


def load_scheduler_config(
    path: str | Path = DEFAULT_CONFIG,
) -> tuple[IntakeSchedulerConfig, Path, Path]:
    config_path = Path(path).resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    config = IntakeSchedulerConfig.model_validate(raw)
    payload = config.model_dump(mode="json", exclude={"config_hash"})
    config.config_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    project_root = config_path.parents[1]
    return config, config_path, project_root


def scheduler_status(path: str | Path = DEFAULT_CONFIG) -> dict[str, object]:
    config, config_path, project_root = load_scheduler_config(path)
    compose_file = _project_path(project_root, config.compose_file)
    actors_file = _project_path(project_root, config.actors_file)
    filter_config = _project_path(project_root, config.filter_config)
    production_config = _project_path(project_root, config.production_config)
    discovery_config = _project_path(project_root, config.discovery_config)
    docker_executable = _docker_executable()
    return {
        "status": "configured",
        "task_name": config.task_name,
        "timezone": config.timezone,
        "daily_time": config.daily_time,
        "latest_start_minutes": config.latest_start_minutes,
        "require_ac_power": config.require_ac_power,
        "candidate_target": config.candidate_target,
        "candidates_per_actor": config.candidates_per_actor,
        "actor_count": _actor_count(actors_file) if actors_file.is_file() else 0,
        "actors_file_exists": actors_file.is_file(),
        "filter_config": str(filter_config),
        "filter_config_exists": filter_config.is_file(),
        "production_config": str(production_config),
        "production_config_exists": production_config.is_file(),
        "discovery_enabled": config.discovery_enabled,
        "discovery_config": str(discovery_config),
        "discovery_config_exists": discovery_config.is_file(),
        "compose_file_exists": compose_file.is_file(),
        "docker_available": docker_executable is not None,
        "docker_executable": docker_executable,
        "free_gb": round(shutil.disk_usage(project_root).free / (1024**3), 2),
        "config": str(config_path),
        "config_hash": config.config_hash,
    }


def run_intake(
    path: str | Path = DEFAULT_CONFIG,
    *,
    due_only: bool,
    now: datetime | None = None,
) -> dict[str, object]:
    config, config_path, project_root = load_scheduler_config(path)
    current = now or datetime.now(ZoneInfo(config.timezone))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo(config.timezone))
    else:
        current = current.astimezone(ZoneInfo(config.timezone))
    if due_only and not _inside_start_window(current, config):
        return _record_scheduler_event(
            project_root,
            current,
            {
                "status": "skipped",
                "reason": "outside_start_window",
                "scheduled_time": config.daily_time,
                "latest_start_minutes": config.latest_start_minutes,
            },
        )

    compose_file = _project_path(project_root, config.compose_file)
    actors_file = _project_path(project_root, config.actors_file)
    filter_config = _project_path(project_root, config.filter_config)
    production_config = _project_path(project_root, config.production_config)
    discovery_config = _project_path(project_root, config.discovery_config)
    _preflight(
        config,
        project_root,
        compose_file,
        actors_file,
        filter_config,
        production_config,
        discovery_config,
    )
    docker_executable = _docker_executable()
    if docker_executable is None:
        raise RuntimeError("Docker Desktop or a native docker CLI is not available in WSL")
    run_id = (
        f"scheduled__{current.date().isoformat()}"
        if due_only
        else f"manual__{current.strftime('%Y%m%dT%H%M%S%z')}"
    )
    lock_path = project_root / "data/production/intake-scheduler.lock"
    with _exclusive_lock(lock_path):
        _record_scheduler_event(
            project_root,
            current,
            {"status": "starting", "run_id": run_id, "candidate_target": config.candidate_target},
        )
        _ensure_docker_engine(
            docker_executable,
            timeout=config.startup_timeout_seconds,
            poll_seconds=min(config.poll_seconds, 5),
        )
        compose = _compose_command(compose_file, docker_executable)
        environment = {**os.environ, "AIRFLOW_UID": str(os.getuid())}
        _run_command(
            [*compose, "up", "-d", "--build", "qdrant", "airflow"],
            cwd=project_root,
            env=environment,
            timeout=config.startup_timeout_seconds,
        )
        airflow = [*compose, "exec", "-T", "airflow", "airflow"]
        _wait_for_dag(
            airflow,
            config.dag_id,
            cwd=project_root,
            env=environment,
            timeout=config.startup_timeout_seconds,
            poll_seconds=min(config.poll_seconds, 10),
        )
        _run_command(
            [*airflow, "dags", "unpause", "-y", config.dag_id],
            cwd=project_root,
            env=environment,
            timeout=120,
        )
        existing_state = _dag_state(
            airflow,
            config.dag_id,
            run_id,
            cwd=project_root,
            env=environment,
        )
        if existing_state is None:
            dag_conf = json.dumps(
                {
                    "actors_file": str(config.actors_file),
                    "filter_config": str(config.filter_config),
                    "candidate_target": config.candidate_target,
                    "max_candidates_per_actor": config.candidates_per_actor,
                    "production_config": str(config.production_config),
                    "discovery_enabled": config.discovery_enabled,
                    "discovery_config": str(config.discovery_config),
                    "discovery_creator_budget": config.discovery_creator_budget,
                    "discovery_max_tasks": config.discovery_max_tasks,
                },
                separators=(",", ":"),
            )
            _run_command(
                [
                    *airflow,
                    "dags",
                    "trigger",
                    "-r",
                    run_id,
                    "-c",
                    dag_conf,
                    config.dag_id,
                ],
                cwd=project_root,
                env=environment,
                timeout=120,
            )
        final_state = _wait_for_run(
            airflow,
            config.dag_id,
            run_id,
            cwd=project_root,
            env=environment,
            timeout=config.run_timeout_seconds,
            poll_seconds=config.poll_seconds,
        )
        report = {
            "status": final_state,
            "run_id": run_id,
            "candidate_target": config.candidate_target,
            "candidates_per_actor": config.candidates_per_actor,
            "actor_count": _actor_count(actors_file),
            "discovery_enabled": config.discovery_enabled,
            "config_hash": config.config_hash,
        }
        _record_scheduler_event(project_root, current, report)
        if final_state != "success":
            raise RuntimeError(f"Airflow intake {run_id} finished with state {final_state}")
        return report


def install_windows_task(path: str | Path = DEFAULT_CONFIG) -> dict[str, object]:
    config, config_path, project_root = load_scheduler_config(path)
    if not _running_under_wsl():
        raise RuntimeError("Windows task installation is available only from WSL")
    executable = project_root / ".venv/bin/artsearch-intake"
    if not executable.is_file():
        raise FileNotFoundError("artsearch-intake is not installed; run pip install -e .")
    distro = os.environ.get("WSL_DISTRO_NAME", "")
    action_arguments = []
    if distro:
        action_arguments.extend(["--distribution", _windows_quote(distro)])
    action_arguments.extend(
        [
            "--cd",
            _windows_quote(str(project_root)),
            "--exec",
            _windows_quote(str(executable)),
            "--config",
            _windows_quote(str(config_path)),
            "run-due",
        ]
    )
    script = _windows_install_script(config, " ".join(action_arguments))
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return {
        "status": "installed",
        "task_name": config.task_name,
        "daily_time": config.daily_time,
        "timezone": config.timezone,
        "candidate_target": config.candidate_target,
    }


def uninstall_windows_task(path: str | Path = DEFAULT_CONFIG) -> dict[str, object]:
    config, _, _ = load_scheduler_config(path)
    script = (
        f"Unregister-ScheduledTask -TaskName {_powershell_quote(config.task_name)} "
        "-Confirm:$false -ErrorAction SilentlyContinue"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return {"status": "uninstalled", "task_name": config.task_name}


def _preflight(
    config: IntakeSchedulerConfig,
    project_root: Path,
    compose_file: Path,
    actors_file: Path,
    filter_config: Path,
    production_config: Path,
    discovery_config: Path,
) -> None:
    if _docker_executable() is None:
        raise RuntimeError("Docker Desktop or a native docker CLI is not available in WSL")
    if not compose_file.is_file():
        raise FileNotFoundError(f"Airflow Compose file not found: {compose_file}")
    if not actors_file.is_file() or _actor_count(actors_file) == 0:
        raise FileNotFoundError(f"actor roster is missing or empty: {actors_file}")
    if not filter_config.is_file():
        raise FileNotFoundError(f"artwork-filter config is missing: {filter_config}")
    if not production_config.is_file():
        raise FileNotFoundError(f"production config is missing: {production_config}")
    if config.discovery_enabled and not discovery_config.is_file():
        raise FileNotFoundError(f"discovery config is missing: {discovery_config}")
    free_gb = shutil.disk_usage(project_root).free / (1024**3)
    if free_gb < config.minimum_free_gb:
        raise RuntimeError(
            f"only {free_gb:.1f} GiB free; intake requires {config.minimum_free_gb} GiB"
        )


def _wait_for_dag(
    airflow: list[str],
    dag_id: str,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    poll_seconds: int,
) -> None:
    deadline = time_module.monotonic() + timeout
    while time_module.monotonic() < deadline:
        completed = subprocess.run(
            [*airflow, "dags", "details", dag_id],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return
        time_module.sleep(poll_seconds)
    raise RuntimeError(f"Airflow did not load DAG {dag_id!r} within {timeout} seconds")


def _wait_for_run(
    airflow: list[str],
    dag_id: str,
    run_id: str,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    poll_seconds: int,
) -> str:
    deadline = time_module.monotonic() + timeout
    while time_module.monotonic() < deadline:
        state = _dag_state(airflow, dag_id, run_id, cwd=cwd, env=env)
        if state in TERMINAL_DAG_STATES:
            return state
        time_module.sleep(poll_seconds)
    raise RuntimeError(f"Airflow intake {run_id} exceeded its {timeout}-second timeout")


def _dag_state(
    airflow: list[str],
    dag_id: str,
    run_id: str,
    *,
    cwd: Path,
    env: dict[str, str],
) -> str | None:
    completed = subprocess.run(
        [*airflow, "dags", "state", dag_id, run_id],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    for line in reversed(completed.stdout.splitlines()):
        normalized = line.strip().lower()
        if normalized in ACTIVE_DAG_STATES:
            return normalized
    return None


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {detail[-3000:]}")
    return completed


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another ArtSearch intake launcher is already running") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _record_scheduler_event(
    project_root: Path,
    current: datetime,
    payload: dict[str, object],
) -> dict[str, object]:
    record = {"timestamp": current.isoformat(), **payload}
    path = project_root / "data/production/scheduler" / f"{current.date().isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record


def _inside_start_window(current: datetime, config: IntakeSchedulerConfig) -> bool:
    scheduled = datetime.combine(
        current.date(),
        _parse_daily_time(config.daily_time),
        tzinfo=current.tzinfo,
    )
    return scheduled <= current <= scheduled + timedelta(minutes=config.latest_start_minutes)


def _parse_daily_time(value: str) -> time:
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("daily_time must use 24-hour HH:MM format") from exc
    return parsed


def _actor_count(path: Path) -> int:
    return len(read_actor_roster(path))


def _project_path(project_root: Path, value: Path) -> Path:
    resolved = value.resolve() if value.is_absolute() else (project_root / value).resolve()
    if not resolved.is_relative_to(project_root.resolve()):
        raise ValueError(f"scheduler path must remain under {project_root}: {value}")
    return resolved


def _compose_command(compose_file: Path, executable: str | None = None) -> list[str]:
    executable = executable or _docker_executable()
    if executable is None:
        raise RuntimeError("Docker Desktop or a native docker CLI is not available in WSL")
    return [executable, "compose", "-f", str(compose_file)]


def _ensure_docker_engine(
    executable: str,
    *,
    timeout: int,
    poll_seconds: int,
) -> None:
    if _docker_engine_available(executable):
        return
    if not (_running_under_wsl() and DOCKER_DESKTOP_WINDOWS_CLI.is_file()):
        raise RuntimeError("docker CLI is installed, but its engine is unavailable")

    completed = subprocess.run(
        [str(DOCKER_DESKTOP_WINDOWS_CLI), "desktop", "start"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"could not start Docker Desktop: {detail[-2000:]}")

    deadline = time_module.monotonic() + timeout
    while time_module.monotonic() < deadline:
        if _docker_engine_available(executable):
            return
        time_module.sleep(poll_seconds)
    raise RuntimeError(f"Docker Desktop did not become ready within {timeout} seconds")


def _docker_engine_available(executable: str) -> bool:
    try:
        completed = subprocess.run(
            [executable, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _docker_executable() -> str | None:
    native = shutil.which("docker") or shutil.which("docker.exe")
    if native:
        return native
    if _running_under_wsl() and DOCKER_DESKTOP_WINDOWS_CLI.is_file():
        return str(DOCKER_DESKTOP_WINDOWS_CLI)
    return None


def _windows_install_script(config: IntakeSchedulerConfig, action_arguments: str) -> str:
    hours = max(1, (config.run_timeout_seconds + 3599) // 3600)
    battery_settings = (
        "-AllowStartIfOnBatteries:$false -DontStopIfGoingOnBatteries:$false"
        if config.require_ac_power
        else "-AllowStartIfOnBatteries:$true -DontStopIfGoingOnBatteries:$true"
    )
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "$action = New-ScheduledTaskAction -Execute 'wsl.exe' "
            f"-Argument {_powershell_quote(action_arguments)}",
            f"$trigger = New-ScheduledTaskTrigger -Daily -At {_powershell_quote(config.daily_time)}",
            "$settings = New-ScheduledTaskSettingsSet "
            "-StartWhenAvailable:$false -WakeToRun:$false -MultipleInstances IgnoreNew "
            f"{battery_settings} "
            f"-ExecutionTimeLimit (New-TimeSpan -Hours {hours}) "
            f"-RestartCount {config.restart_count} "
            f"-RestartInterval (New-TimeSpan -Minutes {config.restart_interval_minutes})",
            "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME "
            "-LogonType Interactive -RunLevel Limited",
            f"Register-ScheduledTask -TaskName {_powershell_quote(config.task_name)} "
            "-Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force "
            "| Out-Null",
        ]
    )


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _running_under_wsl() -> bool:
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.lower() or "wsl" in release.lower()


if __name__ == "__main__":
    main()
