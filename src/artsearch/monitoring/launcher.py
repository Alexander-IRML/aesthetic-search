from __future__ import annotations

from pathlib import Path
import shlex
import stat
import subprocess


def install_desktop_launcher(project_root: Path) -> Path:
    root = project_root.resolve()
    executable = root / ".venv/bin/artsearch-model-dashboard"
    if not executable.exists():
        raise FileNotFoundError(
            "artsearch-model-dashboard is not installed; run pip install -e . first"
        )
    if _running_under_wsl():
        return _install_windows_wsl_launcher(root)
    return _install_freedesktop_launcher(root)


def _install_windows_wsl_launcher(root: Path) -> Path:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[Environment]::GetFolderPath('Desktop')",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    windows_desktop = completed.stdout.strip().replace("\r", "")
    if not windows_desktop:
        raise RuntimeError("Windows Desktop path could not be resolved")
    desktop = Path(subprocess.check_output(["wslpath", "-u", windows_desktop], text=True).strip())
    desktop.mkdir(parents=True, exist_ok=True)
    destination = desktop / "ArtSearch Model Dashboard.cmd"
    destination.write_text(
        _windows_batch_content(root),
        encoding="utf-8",
        newline="",
    )
    return destination


def _windows_batch_content(root: Path) -> str:
    executable = root / ".venv/bin/artsearch-model-dashboard"
    command = f'wsl.exe --cd "{root}" --exec "{executable}"'
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        "title ArtSearch Model Operations\r\n"
        "echo Starting ArtSearch Model Operations...\r\n"
        f"echo Project: {root}\r\n"
        "echo The dashboard will open in your browser when it is ready.\r\n"
        "echo.\r\n"
        f"{command}\r\n"
        'set "ARTSEARCH_EXIT=%ERRORLEVEL%"\r\n'
        'if "%ARTSEARCH_EXIT%"=="0" exit /b 0\r\n'
        "echo.\r\n"
        "echo ERROR: ArtSearch could not start. Exit code %ARTSEARCH_EXIT%.\r\n"
        "echo Reinstall with: .venv/bin/artsearch-model-dashboard "
        "--install-desktop-launcher\r\n"
        "pause\r\n"
        "exit /b %ARTSEARCH_EXIT%\r\n"
    )


def _install_freedesktop_launcher(root: Path) -> Path:
    desktop = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    destination = desktop / "artsearch-model-dashboard.desktop"
    shell_command = f"cd {shlex.quote(str(root))} && exec .venv/bin/artsearch-model-dashboard"
    destination.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ArtSearch Model Dashboard\n"
        "Comment=Open the local ArtSearch model operations dashboard\n"
        f"Exec=/bin/bash -lc {shlex.quote(shell_command)}\n"
        "Terminal=true\n"
        "Categories=Development;\n",
        encoding="utf-8",
    )
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
    return destination


def _running_under_wsl() -> bool:
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.lower() or "wsl" in release.lower()
