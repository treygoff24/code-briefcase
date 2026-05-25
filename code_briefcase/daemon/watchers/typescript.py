"""TypeScript diagnostics watcher backed by ``tsc --watch``."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any

from code_briefcase.command_exec import expand_shebang_command
from code_briefcase.diagnostics import (
    _filter_diagnostics_to_file,
    _find_js_ts_project_config,
    _parse_tsc_output,
    _resolve_tool,
)
from code_briefcase.tsc_cache import tsc_version

from .base import (
    AdapterCapability,
    AdapterHealth,
    AdapterKey,
    CanStartResult,
    FileVersion,
    QueryResponse,
    QueryStatus,
    WatchAdapter,
)

WATCH_END_RE = re.compile(r"Found\s+(\d+)\s+errors?\. Watching for file changes\.", re.I)
CHANGE_RE = re.compile(r"(File change detected|Starting compilation in watch mode)", re.I)
PROJECT_FILES_TIMEOUT_ENV = "CODE_BRIEFCASE_WATCH_PROJECT_FILES_TIMEOUT_MS"
TRUST_REPO_BINARIES_ENV = "CODE_BRIEFCASE_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES"
LEGACY_TRUST_REPO_BINARIES_ENV = "TLDR_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES"
TRUE_VALUES = {"1", "true", "yes", "on"}


def can_start_typescript(
    file_path: Path,
    *,
    allow_js: bool,
    project: Path | None = None,
) -> CanStartResult:
    tsc = _resolve_tool("tsc", file_path)
    if not tsc:
        return CanStartResult(ok=False, reason="tsc_not_found")

    tsc_path = Path(tsc).resolve()
    if project is not None and _is_repo_local_node_binary(tsc_path, project) and not _trust_repo_binaries():
        return CanStartResult(ok=False, reason="untrusted_repo_binary")

    config = _find_js_ts_project_config(file_path)
    if not config:
        return CanStartResult(ok=False, reason="tsconfig_not_found")

    version = tsc_version(tsc)
    mode = "allowjs" if allow_js else "noemit"
    return CanStartResult(
        ok=True,
        key=AdapterKey(
            language="javascript" if allow_js else "typescript",
            tool_path=tsc_path,
            config_path=config.resolve(),
            mode=mode,
        ),
        version=version,
    )


def sweep_orphan_watchers(project: Path) -> int:
    """Best-effort cleanup for tsc watchers orphaned by unclean daemon exits."""
    entries = _read_registry(project)
    kept: list[dict[str, Any]] = []
    stopped = 0
    for entry in entries:
        pid = _entry_pid(entry)
        if pid is None or not _is_process_running(pid):
            continue
        if _looks_like_registered_watcher(pid, entry):
            _terminate_process_group(pid)
            stopped += 1
            continue
        kept.append(entry)
    _write_registry(project, kept)
    return stopped


class TypeScriptWatchAdapter(WatchAdapter):
    CAPABILITY = AdapterCapability.COMPILER_WATCH_TEXT
    LANGUAGE = "typescript"

    def __init__(self, key: AdapterKey, *, project: Path) -> None:
        self.key = key
        self.project = project.resolve()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._started_at: float | None = None
        self._stopping = False
        self._unhealthy_reason: str | None = None
        self._batch_seq = 0
        self._last_check_at: float | None = None
        self._diagnostics_by_file: dict[Path, list[dict[str, Any]]] = {}
        self._covered_mtime_by_file: dict[Path, int] = {}
        self._pending_versions: dict[Path, FileVersion] = {}
        self._uncovered_versions: dict[Path, FileVersion] = {}
        self._project_files: set[Path] | None = None
        self._pid_registered = False

    def start(self) -> None:
        with self._lock:
            if self._process is not None or self._unhealthy_reason:
                return
            self._stopping = False
            env = os.environ.copy()
            env.update({"LC_ALL": "C", "LANG": "C", "TZ": "UTC"})
            self._project_files = self._load_project_files(env)
            command = self._watch_command()
            kwargs: dict[str, Any] = {
                "cwd": str(self.key.config_path.parent),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
                "env": env,
            }
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            try:
                self._process = subprocess.Popen(expand_shebang_command(command), **kwargs)
            except OSError as exc:
                self._unhealthy_reason = f"spawn_failed:{exc.__class__.__name__}"
                self._record_event("unhealthy", status="unhealthy", error_kind=exc.__class__.__name__)
                self._condition.notify_all()
                return
            self._started_at = time.time()
            self._register_process(command)
            self._record_event("start", status="running")
            self._reader = threading.Thread(
                target=self._read_output,
                name=f"code-briefcase-tsc-watch-{self.key.config_path.name}",
                daemon=True,
            )
            self._reader.start()

    def notify_edit(self, file_path: Path, version: FileVersion) -> None:
        target = file_path.resolve()
        with self._condition:
            self._pending_versions[target] = version
            self._uncovered_versions.pop(target, None)
            self._batch_seq += 1
            self._condition.notify_all()

    def query(
        self,
        file_path: Path,
        version: FileVersion,
        *,
        budget_ms: int,
    ) -> QueryResponse:
        started = time.perf_counter()
        target = file_path.resolve()
        deadline = time.monotonic() + max(0, budget_ms) / 1000
        with self._condition:
            while True:
                if self._unhealthy_reason:
                    return self._response(
                        QueryStatus.UNHEALTHY,
                        target,
                        started,
                        fallback_reason=self._unhealthy_reason,
                    )
                fallback_reason = self._coverage_fallback_reason_locked(target, version)
                if fallback_reason:
                    return self._response(
                        QueryStatus.FALLBACK_REQUIRED,
                        target,
                        started,
                        fallback_reason=fallback_reason,
                    )
                if self._is_fresh_locked(target, version):
                    return self._response(QueryStatus.FRESH, target, started)

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = (
                        QueryStatus.STALE
                        if target in self._diagnostics_by_file
                        else QueryStatus.PENDING
                    )
                    return self._response(status, target, started)
                self._condition.wait(timeout=min(remaining, 0.1))

    def stop(self, grace_ms: int = 3000) -> None:
        process: subprocess.Popen[str] | None
        with self._condition:
            self._stopping = True
            process = self._process
            self._process = None
            self._condition.notify_all()
        if process is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass
        try:
            process.wait(timeout=max(0.1, grace_ms / 1000))
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":  # pragma: no cover
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                process.kill()
        finally:
            self._unregister_process(process.pid)

    def health(self) -> AdapterHealth:
        with self._lock:
            pid = self._process.pid if self._process is not None else None
            if self._unhealthy_reason:
                status = "unhealthy"
                message = self._unhealthy_reason
            elif self._process is not None:
                status = "running"
                message = None
            else:
                status = "stopped"
                message = None
            return AdapterHealth(
                status=status,
                message=message,
                pid=pid,
                batch_seq=self._batch_seq,
                started_at=self._started_at,
            )

    def _watch_command(self) -> list[str]:
        command = [
            str(self.key.tool_path),
            "--noEmit",
            "--watch",
            "--pretty",
            "false",
            "--project",
            str(self.key.config_path),
        ]
        if self.key.mode == "allowjs":
            command.append("--allowJs")
        return command

    def _load_project_files(self, env: dict[str, str]) -> set[Path] | None:
        command = [
            str(self.key.tool_path),
            "--noEmit",
            "--listFilesOnly",
            "--pretty",
            "false",
            "--project",
            str(self.key.config_path),
        ]
        if self.key.mode == "allowjs":
            command.append("--allowJs")
        try:
            result = subprocess.run(
                expand_shebang_command(command),
                cwd=str(self.key.config_path.parent),
                capture_output=True,
                text=True,
                timeout=_project_files_timeout_seconds(),
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        files: set[Path] = set()
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            path = Path(line)
            if not path.is_absolute():
                path = self.key.config_path.parent / path
            try:
                files.add(path.resolve())
            except OSError:
                continue
        return files

    def _read_output(self) -> None:
        buffer: list[str] = []
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                if CHANGE_RE.search(line):
                    with self._condition:
                        self._batch_seq += 1
                    buffer = []
                    continue
                match = WATCH_END_RE.search(line)
                if match:
                    self._complete_batch(buffer, expected_errors=int(match.group(1)))
                    buffer = []
                    continue
                buffer.append(line)
        finally:
            return_code = process.poll()
            with self._condition:
                if not self._stopping and return_code not in (None, 0):
                    self._unhealthy_reason = f"tsc_exited:{return_code}"
                    self._record_event(
                        "exit",
                        status="unhealthy",
                        exit_code=return_code,
                        error_kind="tsc_exited",
                    )
                self._process = None
                self._condition.notify_all()

    def _complete_batch(self, buffer: list[str], *, expected_errors: int) -> None:
        output = "\n".join(buffer)
        diagnostics = _parse_tsc_output(output)
        if expected_errors != len(diagnostics):
            with self._condition:
                self._unhealthy_reason = (
                    f"parser_mismatch:expected_{expected_errors}_parsed_{len(diagnostics)}"
                )
                self._record_event(
                    "unhealthy",
                    status="unhealthy",
                    batch_seq=self._batch_seq,
                    error_kind="parser_mismatch",
                )
                self._condition.notify_all()
            return

        cwd = self.key.config_path.parent
        by_file: dict[Path, list[dict[str, Any]]] = {}
        for diagnostic in diagnostics:
            raw = diagnostic.get("file")
            if not raw:
                continue
            path = Path(str(raw))
            if not path.is_absolute():
                path = cwd / path
            resolved = path.resolve()
            normalized = dict(diagnostic)
            normalized["file"] = str(resolved)
            by_file.setdefault(resolved, []).append(normalized)

        now = time.time()
        with self._condition:
            self._last_check_at = now
            previous_paths = set(self._diagnostics_by_file)
            for path in set(by_file):
                self._diagnostics_by_file[path] = by_file.get(path, [])
                pending = self._pending_versions.pop(path, None)
                if pending is not None:
                    self._covered_mtime_by_file[path] = pending.mtime_ns
                else:
                    try:
                        self._covered_mtime_by_file[path] = path.stat().st_mtime_ns
                    except OSError:
                        self._covered_mtime_by_file[path] = time.time_ns()
                self._uncovered_versions.pop(path, None)

            for path in previous_paths - set(by_file):
                self._diagnostics_by_file[path] = []
                try:
                    self._covered_mtime_by_file[path] = path.stat().st_mtime_ns
                except OSError:
                    self._covered_mtime_by_file[path] = time.time_ns()
                self._uncovered_versions.pop(path, None)

            for path, pending in list(self._pending_versions.items()):
                if self._is_known_project_file_locked(path):
                    self._diagnostics_by_file[path] = []
                    self._covered_mtime_by_file[path] = pending.mtime_ns
                    self._pending_versions.pop(path, None)
                    self._uncovered_versions.pop(path, None)
                else:
                    self._uncovered_versions[path] = pending
            self._condition.notify_all()
        self._record_event(
            "recheck_complete",
            duration_ms=0,
            status="fresh",
            batch_seq=self._batch_seq,
            queue_depth=len(self._pending_versions),
        )

    def _is_fresh_locked(self, target: Path, version: FileVersion) -> bool:
        covered = self._covered_mtime_by_file.get(target)
        return covered is not None and covered >= version.mtime_ns

    def _is_known_project_file_locked(self, target: Path) -> bool:
        return self._project_files is not None and target.resolve() in self._project_files

    def _coverage_fallback_reason_locked(
        self,
        target: Path,
        version: FileVersion,
    ) -> str | None:
        if self._project_files is not None and target.resolve() not in self._project_files:
            return "not_in_project_config"
        uncovered = self._uncovered_versions.get(target)
        if uncovered is not None and uncovered.mtime_ns >= version.mtime_ns:
            return "not_in_project_config"
        return None

    def _response(
        self,
        status: QueryStatus,
        target: Path,
        started: float,
        *,
        fallback_reason: str | None = None,
    ) -> QueryResponse:
        diagnostics = list(self._diagnostics_by_file.get(target, []))
        if status in {QueryStatus.FRESH, QueryStatus.STALE}:
            diagnostics = _filter_diagnostics_to_file(
                diagnostics,
                target,
                self.key.config_path.parent,
            )
        age_ms = None
        if self._last_check_at is not None:
            age_ms = max(0, int((time.time() - self._last_check_at) * 1000))
        return QueryResponse(
            status=status,
            diagnostics=diagnostics,
            batch_seq=self._batch_seq,
            last_check_at=self._last_check_at,
            age_ms=age_ms,
            wait_ms=int((time.perf_counter() - started) * 1000),
            fallback_reason=fallback_reason,
            backend="tsc-watch",
        )

    def _record_event(
        self,
        action: str,
        *,
        duration_ms: int | None = None,
        status: str | None = None,
        batch_seq: int | None = None,
        queue_depth: int | None = None,
        exit_code: int | None = None,
        error_kind: str | None = None,
    ) -> None:
        try:
            from code_briefcase.telemetry import record_watch_diagnostics_event

            record_watch_diagnostics_event(
                project=self.project,
                action=action,
                adapter_key=self.key.stable_id(),
                duration_ms=duration_ms,
                status=status,
                batch_seq=batch_seq,
                queue_depth=queue_depth,
                exit_code=exit_code,
                error_kind=error_kind,
            )
        except Exception:
            return

    def _register_process(self, command: list[str]) -> None:
        process = self._process
        if process is None:
            return
        entries = [
            entry
            for entry in _read_registry(self.project)
            if _entry_pid(entry) != process.pid
        ]
        entries.append(
            {
                "pid": process.pid,
                "tool_path": str(self.key.tool_path),
                "config_path": str(self.key.config_path),
                "command": command,
                "started_at": self._started_at,
            }
        )
        _write_registry(self.project, entries)
        self._pid_registered = True

    def _unregister_process(self, pid: int) -> None:
        if not self._pid_registered:
            return
        entries = [
            entry
            for entry in _read_registry(self.project)
            if _entry_pid(entry) != pid
        ]
        _write_registry(self.project, entries)
        self._pid_registered = False


def _trust_repo_binaries() -> bool:
    raw = os.environ.get(TRUST_REPO_BINARIES_ENV)
    if raw is None:
        raw = os.environ.get(LEGACY_TRUST_REPO_BINARIES_ENV)
    return raw is not None and raw.strip().lower() in TRUE_VALUES


def _is_repo_local_node_binary(tool_path: Path, project: Path) -> bool:
    try:
        resolved_tool = tool_path.resolve()
        resolved_project = project.resolve()
        resolved_tool.relative_to(resolved_project)
    except (OSError, ValueError):
        return False
    return "node_modules" in resolved_tool.parts


def _project_files_timeout_seconds() -> float:
    raw = os.environ.get(PROJECT_FILES_TIMEOUT_ENV)
    if not raw:
        return 0.75
    try:
        return min(15.0, max(0.05, int(raw) / 1000))
    except ValueError:
        return 0.75


def _registry_path(project: Path) -> Path:
    digest = hashlib.md5(str(project.resolve()).encode()).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"code-briefcase-tsc-watch-{digest}.json"


def _read_registry(project: Path) -> list[dict[str, Any]]:
    path = _registry_path(project)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = payload.get("watchers") if isinstance(payload, dict) else None
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _write_registry(project: Path, entries: list[dict[str, Any]]) -> None:
    path = _registry_path(project)
    try:
        if entries:
            path.write_text(json.dumps({"watchers": entries}, indent=2) + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()
    except OSError:
        return


def _entry_pid(entry: dict[str, Any]) -> int | None:
    try:
        return int(entry.get("pid"))
    except (TypeError, ValueError):
        return None


def _is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _process_command(pid: int) -> str | None:
    if os.name == "nt":  # pragma: no cover - best-effort POSIX sweep only for now
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _looks_like_registered_watcher(pid: int, entry: dict[str, Any]) -> bool:
    command = _process_command(pid)
    config_path = str(entry.get("config_path") or "")
    if not command or not config_path:
        return False
    return "--watch" in command and config_path in command


def _terminate_process_group(pid: int) -> None:
    try:
        if os.name == "nt":  # pragma: no cover
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            return
