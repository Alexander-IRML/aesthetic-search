from datetime import datetime
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

from artsearch.production.scheduler import (
    DOCKER_DESKTOP_WINDOWS_CLI,
    IntakeSchedulerConfig,
    _docker_executable,
    _ensure_docker_engine,
    _inside_start_window,
    _windows_install_script,
    load_scheduler_config,
    run_intake,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_default_scheduler_targets_five_thousand_candidates():
    config, _, project_root = load_scheduler_config(
        PROJECT_ROOT / "configs/intake_scheduler.default.toml"
    )

    assert project_root == PROJECT_ROOT
    assert config.candidate_target == 5000
    assert config.candidates_per_actor == 20
    assert config.daily_time == "10:00"
    assert config.latest_start_minutes == 15
    assert config.require_ac_power is True
    assert config.production_config == Path("configs/production.default.toml")
    assert config.filter_config == Path("configs/artwork_filter.default.toml")


def test_bulk_scheduler_targets_private_csv_and_twenty_thousand_candidates():
    config, _, project_root = load_scheduler_config(
        PROJECT_ROOT / "configs/intake_scheduler.bulk.toml"
    )

    assert project_root == PROJECT_ROOT
    assert config.candidate_target == 20000
    assert config.candidates_per_actor == 20
    assert config.actors_file == Path("config/bluesky_actors.local.csv")
    assert config.production_config == Path("configs/production.alpha-cloud.local.toml")
    assert config.filter_config == Path("configs/artwork_filter.bulk.toml")
    assert config.run_timeout_seconds == 129600


def test_due_window_is_closed_after_fifteen_minutes():
    config = IntakeSchedulerConfig()
    timezone = ZoneInfo(config.timezone)

    assert _inside_start_window(datetime(2026, 8, 18, 10, 15, tzinfo=timezone), config)
    assert not _inside_start_window(datetime(2026, 8, 18, 10, 16, tzinfo=timezone), config)


def test_missed_daily_run_is_recorded_without_starting_docker(tmp_path):
    project = tmp_path / "project"
    configs = project / "configs"
    configs.mkdir(parents=True)
    config_path = configs / "intake.toml"
    config_path.write_text(
        """
version = "test"
timezone = "America/Chicago"
daily_time = "10:00"
latest_start_minutes = 15
candidate_target = 5000
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = run_intake(
        config_path,
        due_only=True,
        now=datetime(2026, 8, 18, 11, 0, tzinfo=ZoneInfo("America/Chicago")),
    )

    assert report["status"] == "skipped"
    assert report["reason"] == "outside_start_window"
    assert (project / "data/production/scheduler/2026-08-18.jsonl").is_file()


def test_windows_task_disables_late_start_wake_and_parallel_instances():
    script = _windows_install_script(IntakeSchedulerConfig(), '--exec "artsearch-intake"')

    assert "-StartWhenAvailable:$false" in script
    assert "-WakeToRun:$false" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-AllowStartIfOnBatteries:$false" in script
    assert "-DontStopIfGoingOnBatteries:$false" in script
    assert "-RestartCount 2" in script


def test_docker_desktop_windows_cli_is_detected_under_wsl(monkeypatch, tmp_path):
    docker_cli = tmp_path / "docker.exe"
    docker_cli.touch()
    monkeypatch.setattr(
        "artsearch.production.scheduler.DOCKER_DESKTOP_WINDOWS_CLI",
        docker_cli,
    )
    monkeypatch.setattr("artsearch.production.scheduler.shutil.which", lambda _name: None)
    monkeypatch.setattr("artsearch.production.scheduler._running_under_wsl", lambda: True)

    assert _docker_executable() == str(docker_cli)


def test_native_docker_is_preferred_after_wsl_integration(monkeypatch, tmp_path):
    docker_cli = tmp_path / "docker.exe"
    docker_cli.touch()
    monkeypatch.setattr(
        "artsearch.production.scheduler.DOCKER_DESKTOP_WINDOWS_CLI",
        docker_cli,
    )
    monkeypatch.setattr(
        "artsearch.production.scheduler.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr("artsearch.production.scheduler._running_under_wsl", lambda: True)

    assert _docker_executable() == "/usr/bin/docker"


def test_docker_desktop_constant_targets_the_standard_install_location():
    assert DOCKER_DESKTOP_WINDOWS_CLI.name == "docker.exe"
    assert "Program Files/Docker/Docker" in str(DOCKER_DESKTOP_WINDOWS_CLI)


def test_engine_bootstrap_starts_docker_desktop_when_needed(monkeypatch, tmp_path):
    docker_cli = tmp_path / "docker.exe"
    docker_cli.touch()
    availability = iter([False, True])
    commands = []
    monkeypatch.setattr(
        "artsearch.production.scheduler.DOCKER_DESKTOP_WINDOWS_CLI",
        docker_cli,
    )
    monkeypatch.setattr("artsearch.production.scheduler._running_under_wsl", lambda: True)
    monkeypatch.setattr(
        "artsearch.production.scheduler._docker_engine_available",
        lambda _executable: next(availability),
    )

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="started", stderr="")

    monkeypatch.setattr("artsearch.production.scheduler.subprocess.run", fake_run)

    _ensure_docker_engine("/usr/bin/docker", timeout=30, poll_seconds=1)

    assert commands == [[str(docker_cli), "desktop", "start"]]
