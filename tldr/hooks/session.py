from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tldr.hooks.runtime import HookEvent, HookResponse
from tldr.session_warm import count_source_files
from tldr.tldrignore import ensure_tldrignore


def _log_path(project: Path) -> Path:
    log_dir = project / ".tldr" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "session-warm.log"


def _spawn(command: list[str], project: Path, log_file: Path | None = None) -> None:
    stdout = subprocess.DEVNULL
    stderr = subprocess.DEVNULL
    handle = None
    if log_file is not None:
        handle = log_file.open("ab")
        stdout = handle
        stderr = handle
    try:
        subprocess.Popen(
            command,
            cwd=str(project),
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    except TypeError:
        subprocess.Popen(command, cwd=str(project), stdout=stdout, stderr=stderr)
    finally:
        if handle is not None:
            handle.close()


def build_session_start_response(
    event: HookEvent,
    *,
    max_files: int = 500,
) -> HookResponse:
    project = event.cwd
    if not project.exists() or not project.is_dir():
        return HookResponse.noop()

    actions: list[str] = []
    try:
        created, _ = ensure_tldrignore(project)
        if created:
            actions.append("created .tldrignore")
    except Exception:
        pass

    try:
        _spawn([sys.executable, "-m", "tldr.cli", "daemon", "start", "--project", str(project)], project)
        actions.append("daemon start requested")
    except Exception:
        pass

    try:
        source_count = count_source_files(project, max_count=max_files + 1)
        if source_count <= max_files:
            _spawn(
                [sys.executable, "-m", "tldr.cli", "warm", str(project), "--lang", "all"],
                project,
                _log_path(project),
            )
            actions.append("background warm requested")
        else:
            actions.append(f"skipped warm for large repo ({source_count}+ files)")
    except Exception:
        pass

    if not actions:
        return HookResponse.noop()
    return HookResponse(message="TLDR session hook: " + "; ".join(actions), suppress_output=True)
