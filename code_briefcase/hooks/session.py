from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from code_briefcase.hooks.outcome import HookExecutionResult, noop, ok, skipped
from code_briefcase.hooks.runtime import HookEvent, HookResponse
from code_briefcase.session_warm import count_source_files
from code_briefcase.tldrignore import ensure_tldrignore


def _log_path(project: Path) -> Path:
    log_dir = project / ".code-briefcase" / "logs"
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


def _background_work_enabled() -> bool:
    value = os.environ.get("CODE_BRIEFCASE_SESSION_START_NO_BACKGROUND", "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def build_session_start_response(
    event: HookEvent,
    *,
    max_files: int = 500,
) -> HookExecutionResult:
    project = event.cwd
    if not project.exists() or not project.is_dir():
        return skipped(reason="project_missing")

    actions: list[str] = []
    try:
        created, _ = ensure_tldrignore(project)
        if created:
            actions.append("created .code-briefcaseignore")
    except Exception:
        pass

    if _background_work_enabled():
        try:
            _spawn(
                [
                    sys.executable,
                    "-m",
                    "code_briefcase.cli",
                    "daemon",
                    "start",
                    "--project",
                    str(project),
                ],
                project,
            )
            actions.append("daemon start requested")
        except Exception:
            pass

        try:
            source_count = count_source_files(project, max_count=max_files + 1)
            if source_count <= max_files:
                _spawn(
                    [sys.executable, "-m", "code_briefcase.cli", "warm", str(project), "--lang", "all"],
                    project,
                    _log_path(project),
                )
                actions.append("background warm requested")
            else:
                actions.append(f"skipped warm for large repo ({source_count}+ files)")
        except Exception:
            pass
    else:
        actions.append("background startup disabled")

    if not actions:
        return noop("no_actions")
    return ok(
        HookResponse(message="Code Briefcase session hook: " + "; ".join(actions), suppress_output=True),
        daemon_state="start_requested" if any("daemon" in action for action in actions) else None,
    )
