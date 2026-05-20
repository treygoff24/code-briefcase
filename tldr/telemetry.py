from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from tldr import __version__

DEFAULT_TELEMETRY_PATH = Path.home() / ".tldr" / "telemetry.jsonl"
TELEMETRY_ENABLE_FILE = Path.home() / ".tldr" / "telemetry.enabled"
TELEMETRY_REDACT_PATHS_FILE = Path.home() / ".tldr" / "telemetry.redact_paths"
MAX_TELEMETRY_BYTES = 50 * 1024 * 1024
TELEMETRY_FILE_MODE = 0o600
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def _truthy_env(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return False
    return value in TRUE_VALUES


def _enabled_flag_file(path: Path) -> bool:
    try:
        if not path.exists():
            return False
        value = path.read_text(encoding="utf-8").strip().lower()
        return value not in FALSE_VALUES
    except Exception:
        return False


def telemetry_enabled() -> bool:
    enabled = _truthy_env("TLDR_TELEMETRY")
    if enabled is not None:
        return enabled
    return _enabled_flag_file(TELEMETRY_ENABLE_FILE)


def telemetry_path() -> Path:
    raw = os.environ.get("TLDR_TELEMETRY_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_TELEMETRY_PATH


def redact_paths_enabled() -> bool:
    enabled = _truthy_env("TLDR_TELEMETRY_REDACT_PATHS")
    if enabled is not None:
        return enabled
    return _enabled_flag_file(TELEMETRY_REDACT_PATHS_FILE)


def project_hash(project: Path) -> str:
    return hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:8]


def _normalize_path(project: Path, value: str) -> str:
    if redact_paths_enabled():
        return f"<redacted>/{project_hash(project)}"
    try:
        rel = Path(value).expanduser().resolve().relative_to(project)
        return str(rel)
    except Exception:
        return Path(value).name


def _prepare_paths(project: Path, paths: list[str]) -> list[str]:
    prepared: list[str] = []
    for item in paths:
        if not item:
            continue
        prepared.append(_normalize_path(project, item))
    return prepared


def _rotate_if_needed(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size <= MAX_TELEMETRY_BYTES:
            return
        backup = path.with_suffix(path.suffix + ".1")
        if backup.exists():
            backup.unlink()
        path.rename(backup)
    except Exception:
        return


def _locked_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=TELEMETRY_FILE_MODE)
    try:
        path.chmod(TELEMETRY_FILE_MODE)
    except Exception:
        pass
    _rotate_if_needed(path)
    with path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
        handle.write(line)
        if not line.endswith("\n"):
            handle.write("\n")
        handle.flush()
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def write_telemetry_record(record: dict[str, Any]) -> None:
    if not telemetry_enabled():
        return
    try:
        path = telemetry_path()
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        _locked_append(path, line)
    except Exception:
        return


def record_hook_execution(
    *,
    client: str,
    hook_event: str,
    project: Path,
    duration_ms: int,
    status: str,
    error_kind: str | None = None,
    injected_bytes: int = 0,
    trigger_files: list[str] | None = None,
    recommended_files: list[str] | None = None,
    surfaced_files: list[str] | None = None,
    diagnostics_count: int = 0,
    daemon_state: str | None = None,
    noop_reason: str | None = None,
    session_id: str | None = None,
) -> None:
    project = project.expanduser().resolve()
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
        "version": __version__,
        "client": client,
        "event": hook_event,
        "project": str(project) if not redact_paths_enabled() else f"<redacted>/{project_hash(project)}",
        "project_hash": project_hash(project),
        "duration_ms": duration_ms,
        "status": status,
        "error_kind": error_kind,
        "injected_bytes": injected_bytes,
        "trigger_files": _prepare_paths(project, list(trigger_files or [])),
        "recommended_related_files": _prepare_paths(project, list(recommended_files or [])),
        "surfaced_files": _prepare_paths(project, list(surfaced_files or [])),
        "diagnostics_count": diagnostics_count,
        "daemon_state": daemon_state,
        "noop_reason": noop_reason,
    }
    if session_id:
        record["session_id"] = session_id
    write_telemetry_record(record)
