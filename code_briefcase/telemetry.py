from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from code_briefcase import __version__

DEFAULT_TELEMETRY_PATH = Path.home() / ".code-briefcase" / "telemetry.jsonl"
TELEMETRY_ENABLE_FILE = Path.home() / ".code-briefcase" / "telemetry.enabled"
TELEMETRY_REDACT_PATHS_FILE = Path.home() / ".code-briefcase" / "telemetry.redact_paths"
MAX_TELEMETRY_BYTES = 50 * 1024 * 1024
TELEMETRY_FILE_MODE = 0o600
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
LOCAL_RICH_MODE = "local-rich"
PRIVACY_SAFE_MODE = "privacy-safe"
LOCAL_EVIDENCE_STRING_LIMIT = 8000

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|auth(?:orization)?|bearer|cookie|credential|password|passwd|private[_-]?key|secret|session|token)",
    re.IGNORECASE,
)
SECRET_PATH_RE = re.compile(
    r"(^|[/\\])(\.env(?:\.[^/\\]+)?|id_rsa|id_ed25519|secrets?|credentials?)([/\\]|$)",
    re.IGNORECASE,
)
SECRET_PATH_PARTS = {".env", "secrets", "secret", "credentials", "id_rsa", "id_ed25519"}
SECRET_VALUE_PATTERNS = (
    re.compile(
        r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|authorization|bearer|password|secret|token))=([^ \n\t;&]+)"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|CREDENTIAL)[A-Z0-9_]*\b"),
)


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
    enabled = _truthy_env("CODE_BRIEFCASE_TELEMETRY")
    if enabled is not None:
        return enabled
    return _enabled_flag_file(TELEMETRY_ENABLE_FILE)


def telemetry_mode() -> str:
    raw = os.environ.get("CODE_BRIEFCASE_TELEMETRY_MODE", "").strip().lower()
    if raw in {"rich", "local", "local-rich", "raw"}:
        return LOCAL_RICH_MODE
    return PRIVACY_SAFE_MODE


def local_rich_enabled() -> bool:
    return telemetry_mode() == LOCAL_RICH_MODE


def telemetry_path() -> Path:
    raw = os.environ.get("CODE_BRIEFCASE_TELEMETRY_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_TELEMETRY_PATH


def redact_paths_enabled() -> bool:
    if local_rich_enabled():
        return False
    enabled = _truthy_env("CODE_BRIEFCASE_TELEMETRY_REDACT_PATHS")
    if enabled is not None:
        return enabled
    if _enabled_flag_file(TELEMETRY_REDACT_PATHS_FILE):
        return True
    return True


def project_hash(project: Path) -> str:
    return hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:8]


def _path_key(project: Path, value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project / path
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    try:
        return str(resolved.relative_to(project)).replace("\\", "/")
    except Exception:
        return str(resolved).replace("\\", "/")


def telemetry_path_hash(project: Path, value: str) -> str:
    return hashlib.sha256(_path_key(project, value).encode("utf-8")).hexdigest()[:12]


def _looks_secret_path_value(value: str) -> bool:
    text = value.strip("\"'`")
    if SECRET_PATH_RE.search(text):
        return True
    for part in re.split(r"[/\\]+", text):
        lowered = part.strip(".,:()[]{}\"'`").lower()
        if lowered.startswith(".env") or lowered in SECRET_PATH_PARTS:
            return True
        if "secret" in lowered or "credential" in lowered:
            return True
    return False


def _redact_secret_path(value: str) -> str:
    if _looks_secret_path_value(value):
        return "[redacted-secret-path]"
    return value


def _normalize_path(project: Path, value: str) -> str:
    if redact_paths_enabled():
        return f"<redacted>/{project_hash(project)}/{telemetry_path_hash(project, value)}"
    try:
        local = _redact_secret_path(_path_key(project, value))
        if local == "[redacted-secret-path]":
            return local
        rel = Path(local)
        if rel.is_absolute():
            return rel.name
        return str(rel)
    except Exception:
        return _redact_secret_path(Path(value).name)


def _local_path(project: Path, value: str) -> str:
    try:
        return _redact_secret_path(_path_key(project, value))
    except Exception:
        return _redact_secret_path(str(value))


def _prepare_paths(project: Path, paths: list[str]) -> list[str]:
    prepared: list[str] = []
    for item in paths:
        if not item:
            continue
        prepared.append(_normalize_path(project, item))
    return prepared


def _redact_secret_string(value: str) -> str:
    redacted = "".join(
        _redact_secret_path(token) if token and not token.isspace() else token
        for token in re.split(r"(\s+|[;&|])", value)
    )
    for pattern in SECRET_VALUE_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            if match.lastindex and match.lastindex >= 1:
                prefix = match.group(1)
                if "=" in match.group(0):
                    return f"{prefix}=[redacted]"
            return "[redacted-secret]"

        redacted = pattern.sub(repl, redacted)
    try:
        limit = int(
            os.environ.get("CODE_BRIEFCASE_TELEMETRY_LOCAL_STRING_LIMIT", LOCAL_EVIDENCE_STRING_LIMIT)
        )
    except (TypeError, ValueError):
        limit = LOCAL_EVIDENCE_STRING_LIMIT
    if len(redacted) > limit:
        return redacted[:limit].rstrip() + f"... [truncated {len(redacted) - limit} chars]"
    return redacted


def sanitize_local_evidence(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated-depth]"
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if SECRET_KEY_RE.search(text_key):
                sanitized[text_key] = "[redacted-secret]"
                continue
            sanitized[text_key] = sanitize_local_evidence(item, depth=depth + 1)
        return sanitized
    if isinstance(value, list):
        return [sanitize_local_evidence(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, tuple):
        return [sanitize_local_evidence(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return _redact_secret_string(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_secret_string(str(value))


def _prepare_candidate_files(
    project: Path, candidates: list[dict[str, object]] | None
) -> list[dict[str, object]]:
    prepared: list[dict[str, object]] = []
    for item in candidates or []:
        path = str(item.get("path") or "")
        if not path:
            continue
        entry: dict[str, object] = {
            "path": _normalize_path(project, path),
            "reason": item.get("reason"),
            "rank": item.get("rank"),
            "surfaced": bool(item.get("surfaced")),
        }
        if item.get("score") is not None:
            entry["score"] = item.get("score")
        if item.get("excluded_reason"):
            entry["excluded_reason"] = item.get("excluded_reason")
        prepared.append(entry)
    return prepared


def _prepare_local_candidate_files(
    project: Path, candidates: list[dict[str, object]] | None
) -> list[dict[str, object]]:
    prepared: list[dict[str, object]] = []
    for item in candidates or []:
        path = str(item.get("path") or "")
        entry = dict(item)
        if path:
            entry["path"] = _local_path(project, path)
            entry["path_hash"] = telemetry_path_hash(project, path)
        prepared.append(sanitize_local_evidence(entry))
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
    candidate_files: list[dict[str, object]] | None = None,
    context_kind: str | None = None,
    hook_run_id: str | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> None:
    project = project.expanduser().resolve()
    record: dict[str, Any] = {
        "schema_version": 2,
        "telemetry_mode": telemetry_mode(),
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
        "candidate_files": _prepare_candidate_files(project, candidate_files),
    }
    if session_id:
        record["session_id"] = session_id
    if context_kind:
        record["context_kind"] = context_kind
    if hook_run_id:
        record["hook_run_id"] = hook_run_id
    if local_rich_enabled():
        record["local_evidence"] = {
            "warning": "local-rich evidence may contain private project details; do not commit or share",
            "tool_name": sanitize_local_evidence(tool_name),
            "tool_input": sanitize_local_evidence(tool_input or {}),
            "raw_trigger_files": [_local_path(project, item) for item in list(trigger_files or [])],
            "raw_recommended_related_files": [
                _local_path(project, item) for item in list(recommended_files or [])
            ],
            "raw_surfaced_files": [_local_path(project, item) for item in list(surfaced_files or [])],
            "raw_candidate_files": _prepare_local_candidate_files(project, candidate_files),
        }
    write_telemetry_record(record)
