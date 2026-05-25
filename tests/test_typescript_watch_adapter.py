from __future__ import annotations

import signal
import time
from pathlib import Path

from code_briefcase.daemon.watchers.base import (
    AdapterKey,
    CanStartResult,
    FileVersion,
    QueryResponse,
    QueryStatus,
    file_version,
)
from code_briefcase.daemon.watchers import typescript as typescript_module
from code_briefcase.daemon.watchers.typescript import (
    TypeScriptWatchAdapter,
    can_start_typescript,
    sweep_orphan_watchers,
    _read_registry,
    _write_registry,
)


def _adapter(tmp_path: Path) -> TypeScriptWatchAdapter:
    config = tmp_path / "tsconfig.json"
    config.write_text('{"compilerOptions":{"strict":true}}\n', encoding="utf-8")
    return TypeScriptWatchAdapter(
        AdapterKey(
            language="typescript",
            tool_path=Path("/usr/bin/false"),
            config_path=config,
            mode="noemit",
        ),
        project=tmp_path,
    )


def test_clean_pending_file_only_becomes_fresh_when_project_config_covers_it(tmp_path):
    source = tmp_path / "src" / "covered.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = {source.resolve()}
    version = file_version(source)

    adapter.notify_edit(source, version)
    adapter._complete_batch([], expected_errors=0)

    response = adapter.query(source, version, budget_ms=0)

    assert response.status == QueryStatus.FRESH
    assert response.diagnostics == []


def test_previously_errored_file_is_cleared_when_next_batch_is_clean(tmp_path):
    source = tmp_path / "src" / "fixed.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = {source.resolve()}
    version = file_version(source)
    adapter._diagnostics_by_file[source.resolve()] = [
        {
            "file": str(source.resolve()),
            "line": 1,
            "column": 7,
            "severity": "error",
            "message": "old error",
        }
    ]
    adapter._covered_mtime_by_file[source.resolve()] = version.mtime_ns

    adapter._complete_batch([], expected_errors=0)
    response = adapter.query(source, version, budget_ms=0)

    assert response.status == QueryStatus.FRESH
    assert response.diagnostics == []


def test_clean_pending_file_not_in_project_config_requires_sync_fallback(tmp_path):
    source = tmp_path / "src" / "excluded.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = set()
    version = file_version(source)

    adapter.notify_edit(source, version)
    adapter._complete_batch([], expected_errors=0)

    response = adapter.query(source, version, budget_ms=0)

    assert response.status == QueryStatus.FALLBACK_REQUIRED
    assert response.fallback_reason == "not_in_project_config"


def _fake_tsc(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  echo 'Version 5.0.0'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_repo_local_tsc_watch_requires_explicit_trust(tmp_path, monkeypatch):
    source = tmp_path / "src" / "app.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    _fake_tsc(tmp_path / "node_modules" / ".bin" / "tsc")
    monkeypatch.delenv(
        "CODE_BRIEFCASE_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES", raising=False
    )
    monkeypatch.delenv("TLDR_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES", raising=False)

    untrusted = can_start_typescript(source, allow_js=False, project=tmp_path)

    monkeypatch.setenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES", "1")
    trusted = can_start_typescript(source, allow_js=False, project=tmp_path)

    assert untrusted.ok is False
    assert untrusted.reason == "untrusted_repo_binary"
    assert trusted.ok is True


def test_repo_local_tsc_symlink_target_requires_explicit_trust(tmp_path, monkeypatch):
    source = tmp_path / "src" / "app.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    real_tsc = _fake_tsc(tmp_path / "node_modules" / "typescript" / "bin" / "tsc")
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "tsc").symlink_to(real_tsc)
    monkeypatch.delenv(
        "CODE_BRIEFCASE_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES", raising=False
    )
    monkeypatch.delenv("TLDR_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES", raising=False)

    untrusted = can_start_typescript(source, allow_js=False, project=tmp_path)

    assert untrusted.ok is False
    assert untrusted.reason == "untrusted_repo_binary"


def test_supervisor_refuses_new_adapter_after_cap(tmp_path, monkeypatch):
    from code_briefcase.daemon.watchers import supervisor as supervisor_module

    class FakeAdapter:
        def __init__(self, key: AdapterKey, *, project: Path) -> None:
            self.key = key
            self.project = project

    def fake_can_start(file_path: Path, **_kwargs):
        key = AdapterKey(
            language="typescript",
            tool_path=Path("/usr/bin/tsc"),
            config_path=tmp_path / f"{file_path.stem}.json",
            mode="noemit",
        )
        return CanStartResult(ok=True, key=key)

    monkeypatch.setenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS_MAX_ADAPTERS", "1")
    monkeypatch.setattr(supervisor_module, "can_start_typescript", fake_can_start)
    monkeypatch.setattr(supervisor_module, "TypeScriptWatchAdapter", FakeAdapter)
    supervisor = supervisor_module.WatchSupervisor(tmp_path)

    first = supervisor._adapter_for_file(tmp_path / "one.ts", language="typescript")
    second = supervisor._adapter_for_file(tmp_path / "two.ts", language="typescript")

    assert isinstance(first, FakeAdapter)
    assert isinstance(second, QueryResponse)
    assert second.status == QueryStatus.FALLBACK_REQUIRED
    assert second.fallback_reason == "watcher_limit_exceeded"


def test_unknown_project_coverage_clean_batch_becomes_stale_not_not_in_project(
    tmp_path,
):
    source = tmp_path / "src" / "unknown.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = None
    version = file_version(source)

    adapter.notify_edit(source, version)
    adapter._complete_batch([], expected_errors=0)

    response = adapter.query(source, version, budget_ms=0)

    assert response.status == QueryStatus.STALE
    assert response.diagnostics == []
    assert source.resolve() not in adapter._uncovered_versions


def test_unknown_project_clean_stale_result_returns_without_waiting(
    tmp_path, monkeypatch
):
    source = tmp_path / "src" / "unknown-fast-stale.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = None
    version = file_version(source)

    adapter.notify_edit(source, version)
    adapter._complete_batch([], expected_errors=0)
    monkeypatch.setattr(
        adapter._condition,
        "wait",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(
                "stale known-clean result should not wait for the query budget"
            )
        ),
    )

    response = adapter.query(source, version, budget_ms=1000)

    assert response.status == QueryStatus.STALE


def test_known_excluded_project_file_requires_sync_fallback(tmp_path):
    source = tmp_path / "src" / "excluded.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = set()
    version = file_version(source)

    adapter.notify_edit(source, version)
    adapter._complete_batch([], expected_errors=0)

    response = adapter.query(source, version, budget_ms=0)

    assert response.status == QueryStatus.FALLBACK_REQUIRED
    assert response.fallback_reason == "not_in_project_config"


def test_unknown_project_coverage_does_not_cover_newer_edit_than_batch_snapshot(
    tmp_path, monkeypatch
):
    source = tmp_path / "src" / "racy-unknown.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = None
    first = FileVersion(mtime_ns=100)
    second = FileVersion(mtime_ns=200)

    adapter.notify_edit(source, first)
    monkeypatch.setattr(typescript_module.time, "time_ns", lambda: 150)
    with adapter._condition:
        adapter._mark_batch_started_locked()
    adapter.notify_edit(source, second)
    adapter._complete_batch([], expected_errors=0)

    response = adapter.query(source, second, budget_ms=0)

    assert response.status != QueryStatus.FRESH
    assert response.status != QueryStatus.FALLBACK_REQUIRED
    assert source.resolve() not in adapter._covered_mtime_by_file
    assert adapter._pending_versions[source.resolve()] == second


def test_unknown_project_batch_started_before_notify_clears_older_pending(
    tmp_path, monkeypatch
):
    source = tmp_path / "src" / "notify-race-unknown.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = None
    version = FileVersion(mtime_ns=100)

    monkeypatch.setattr(typescript_module.time, "time_ns", lambda: 150)
    with adapter._condition:
        adapter._mark_batch_started_locked()
    adapter.notify_edit(source, version)
    adapter._complete_batch([], expected_errors=0)

    response = adapter.query(source, version, budget_ms=1000)

    assert response.status == QueryStatus.STALE
    assert source.resolve() not in adapter._pending_versions
    assert source.resolve() not in adapter._covered_mtime_by_file


def test_unknown_project_clean_batch_clears_pending_for_previous_diagnostics(
    tmp_path, monkeypatch
):
    source = tmp_path / "src" / "previous-error-now-clean.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = None
    adapter._diagnostics_by_file[source.resolve()] = [
        {"file": str(source), "message": "old error"}
    ]
    version = FileVersion(mtime_ns=100)

    monkeypatch.setattr(typescript_module.time, "time_ns", lambda: 150)
    with adapter._condition:
        adapter._mark_batch_started_locked()
    adapter.notify_edit(source, version)
    adapter._complete_batch([], expected_errors=0)
    monkeypatch.setattr(
        adapter._condition,
        "wait",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cleared unknown stale result should not wait")
        ),
    )

    response = adapter.query(source, version, budget_ms=1000)

    assert response.status == QueryStatus.STALE
    assert response.diagnostics == []
    assert source.resolve() not in adapter._pending_versions


def test_unknown_project_previous_diagnostics_return_stale_while_pending(
    tmp_path, monkeypatch
):
    source = tmp_path / "src" / "previous-known-pending.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = None
    adapter._diagnostics_by_file[source.resolve()] = []
    version = file_version(source)

    adapter.notify_edit(source, version)
    monkeypatch.setattr(
        adapter._condition,
        "wait",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unknown stale diagnostics should not wait for freshness")
        ),
    )

    response = adapter.query(source, version, budget_ms=1000)

    assert response.status == QueryStatus.STALE
    assert source.resolve() in adapter._pending_versions


def test_unknown_project_coverage_before_batch_completion_stays_pending(tmp_path):
    source = tmp_path / "src" / "warming.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = None
    version = file_version(source)

    adapter.notify_edit(source, version)
    response = adapter.query(source, version, budget_ms=0)

    assert response.status == QueryStatus.PENDING
    assert response.fallback_reason is None


def test_explicit_uncovered_marker_still_requires_sync_fallback(tmp_path):
    source = tmp_path / "src" / "uncovered.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = None
    version = file_version(source)
    adapter._uncovered_versions[source.resolve()] = version

    response = adapter.query(source, version, budget_ms=0)

    assert response.status == QueryStatus.FALLBACK_REQUIRED
    assert response.fallback_reason == "not_in_project_config"


def test_batch_completion_does_not_cover_edit_newer_than_compile_snapshot(tmp_path):
    source = tmp_path / "src" / "racy.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = {source.resolve()}
    first = FileVersion(mtime_ns=100)
    second = FileVersion(mtime_ns=200)

    adapter.notify_edit(source, first)
    with adapter._condition:
        adapter._mark_batch_started_locked()
    adapter.notify_edit(source, second)
    adapter._complete_batch([], expected_errors=0)

    response = adapter.query(source, second, budget_ms=0)

    assert response.status != QueryStatus.FRESH
    assert adapter._covered_mtime_by_file[source.resolve()] == first.mtime_ns
    assert adapter._pending_versions[source.resolve()] == second


def test_non_project_pending_versions_are_cleared_after_batch(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._project_files = set()

    for index in range(25):
        source = tmp_path / "ignored" / f"excluded-{index}.ts"
        source.parent.mkdir(exist_ok=True)
        source.write_text("const answer: number = 42;\n", encoding="utf-8")
        adapter.notify_edit(source, FileVersion(mtime_ns=100 + index))
        adapter._complete_batch([], expected_errors=0)

    assert adapter._pending_versions == {}
    assert len(adapter._uncovered_versions) == 25


class _RunResult:
    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ps_stdout(*, started_at: float, comm: str, command: str) -> str:
    lstart = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(started_at))
    return f"{lstart} {comm} {command}\n"


def test_orphan_sweep_kills_registered_pid_only_when_identity_matches(
    tmp_path,
    monkeypatch,
):
    tool = tmp_path / "node_modules" / ".bin" / "tsc"
    config = tmp_path / "tsconfig.json"
    started_at = 1_800_000_000.0
    entry = {
        "pid": 123,
        "tool_path": str(tool),
        "config_path": str(config),
        "command": [str(tool), "--watch", "--project", str(config)],
        "started_at": started_at,
    }
    command = f"{tool} --noEmit --watch --project {config}"

    monkeypatch.setattr(typescript_module, "_read_registry", lambda _project: [entry])
    written_entries = []
    monkeypatch.setattr(
        typescript_module,
        "_write_registry",
        lambda _project, entries: written_entries.append(entries),
    )
    monkeypatch.setattr(typescript_module, "_is_process_running", lambda _pid: True)
    monkeypatch.setattr(
        typescript_module.subprocess,
        "run",
        lambda *_args, **_kwargs: _RunResult(
            returncode=0,
            stdout=_ps_stdout(started_at=started_at, comm=str(tool), command=command),
        ),
    )
    killed = []
    monkeypatch.setattr(
        typescript_module.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    stopped = sweep_orphan_watchers(tmp_path)

    assert stopped == 1
    assert killed == [(123, signal.SIGTERM)]
    assert written_entries == [[]]


def test_orphan_sweep_does_not_kill_when_registered_start_time_differs(
    tmp_path,
    monkeypatch,
):
    tool = tmp_path / "node_modules" / ".bin" / "tsc"
    config = tmp_path / "tsconfig.json"
    started_at = 1_800_000_000.0
    entry = {
        "pid": 123,
        "tool_path": str(tool),
        "config_path": str(config),
        "command": [str(tool), "--watch", "--project", str(config)],
        "started_at": started_at,
    }
    command = f"{tool} --noEmit --watch --project {config}"

    monkeypatch.setattr(typescript_module, "_read_registry", lambda _project: [entry])
    written_entries = []
    monkeypatch.setattr(
        typescript_module,
        "_write_registry",
        lambda _project, entries: written_entries.append(entries),
    )
    monkeypatch.setattr(typescript_module, "_is_process_running", lambda _pid: True)
    monkeypatch.setattr(
        typescript_module.subprocess,
        "run",
        lambda *_args, **_kwargs: _RunResult(
            returncode=0,
            stdout=_ps_stdout(
                started_at=started_at + 60,
                comm=str(tool),
                command=command,
            ),
        ),
    )
    killed = []
    monkeypatch.setattr(
        typescript_module.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    stopped = sweep_orphan_watchers(tmp_path)

    assert stopped == 0
    assert killed == []
    assert written_entries == [[entry]]


class _FakePopen:
    def __init__(
        self, *, pid: int = 4321, stdout=None, returncode: int | None = None
    ) -> None:
        self.pid = pid
        self.stdout = stdout
        self._returncode = returncode

    def poll(self) -> int | None:
        return self._returncode


def test_unhealthy_adapter_restarts_after_backoff(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    now = [100.0]
    monkeypatch.setattr(typescript_module.time, "monotonic", lambda: now[0])
    adapter._process = _FakePopen(stdout=[], returncode=9)
    adapter._read_output()
    assert adapter.health().status == "unhealthy"

    monkeypatch.setattr(adapter, "_load_project_files", lambda _env: set())
    monkeypatch.setattr(
        typescript_module, "_write_registry", lambda _project, _entries: None
    )
    popen_calls = []

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return _FakePopen(pid=99, stdout=None)

    monkeypatch.setattr(typescript_module.subprocess, "Popen", fake_popen)

    now[0] = 104.9
    adapter.start()
    assert popen_calls == []

    now[0] = 105.0
    adapter.start()

    assert len(popen_calls) == 1
    assert adapter.health().status == "running"
    assert adapter._unhealthy_reason is None


def test_restart_backoff_escalates_after_failed_restart_attempts(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    now = [200.0]
    monkeypatch.setattr(typescript_module.time, "monotonic", lambda: now[0])
    adapter._process = _FakePopen(stdout=[], returncode=9)
    adapter._read_output()
    monkeypatch.setattr(adapter, "_load_project_files", lambda _env: set())
    monkeypatch.setattr(
        typescript_module, "_write_registry", lambda _project, _entries: None
    )
    attempts = []

    def failing_popen(*_args, **_kwargs):
        attempts.append(now[0])
        raise OSError("boom")

    monkeypatch.setattr(typescript_module.subprocess, "Popen", failing_popen)

    now[0] = 205.0
    adapter.start()
    now[0] = 219.0
    adapter.start()
    now[0] = 220.0
    adapter.start()

    assert attempts == [205.0, 220.0]


def test_load_project_files_returns_none_on_tsc_failure(tmp_path, monkeypatch, caplog):
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(
        typescript_module.subprocess,
        "run",
        lambda *_args, **_kwargs: _RunResult(
            returncode=2,
            stdout="",
            stderr="tsconfig failed\n",
        ),
    )

    with caplog.at_level("WARNING"):
        project_files = adapter._load_project_files({})

    assert project_files is None
    assert "tsconfig failed" in caplog.text

    source = tmp_path / "src" / "app.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    version = file_version(source)
    adapter._project_files = project_files
    adapter.notify_edit(source, version)
    adapter._complete_batch([], expected_errors=0)

    response = adapter.query(source, version, budget_ms=0)

    assert response.status == QueryStatus.STALE
    assert response.fallback_reason is None


def test_recheck_complete_telemetry_records_real_batch_duration(tmp_path, monkeypatch):
    source = tmp_path / "src" / "duration.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = {source.resolve()}
    events = []
    monotonic = [100.0]
    monkeypatch.setattr(typescript_module.time, "perf_counter", lambda: monotonic[0])
    monkeypatch.setattr(
        "code_briefcase.telemetry.record_watch_diagnostics_event",
        lambda **kwargs: events.append(kwargs),
    )

    adapter.notify_edit(source, FileVersion(mtime_ns=100))
    with adapter._condition:
        adapter._mark_batch_started_locked()
    monotonic[0] = 101.25
    adapter._complete_batch([], expected_errors=0)

    assert events[-1]["action"] == "recheck_complete"
    assert events[-1]["duration_ms"] == 1250


def test_registry_write_is_atomic_when_replace_fails(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(
        typescript_module, "_registry_path", lambda _project: registry_path
    )
    old_entries = [{"pid": 1, "tool_path": "/bin/old"}]
    new_entries = [{"pid": 2, "tool_path": "/bin/new"}]
    _write_registry(tmp_path, old_entries)

    def fail_replace(_src, _dst):
        raise OSError("simulated interrupted replace")

    monkeypatch.setattr(typescript_module.os, "replace", fail_replace)

    _write_registry(tmp_path, new_entries)

    assert _read_registry(tmp_path) == old_entries
    assert registry_path.with_suffix(".tmp").exists()


def test_recheck_telemetry_uses_completed_compile_batch_seq(tmp_path, monkeypatch):
    source = tmp_path / "src" / "telemetry.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = {source.resolve()}
    events = []
    monkeypatch.setattr(
        "code_briefcase.telemetry.record_watch_diagnostics_event",
        lambda **kwargs: events.append(kwargs),
    )

    adapter.notify_edit(source, FileVersion(mtime_ns=100))
    with adapter._condition:
        adapter._mark_batch_started_locked()
        compile_batch_seq = adapter._batch_seq
    adapter.notify_edit(source, FileVersion(mtime_ns=200))
    adapter._complete_batch([], expected_errors=0)

    assert events[-1]["action"] == "recheck_complete"
    assert events[-1]["batch_seq"] == compile_batch_seq
    assert events[-1]["queue_depth"] == 1
