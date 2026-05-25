from __future__ import annotations

from pathlib import Path

from code_briefcase.daemon.watchers.base import (
    AdapterKey,
    CanStartResult,
    QueryResponse,
    QueryStatus,
    file_version,
)
from code_briefcase.daemon.watchers.typescript import (
    TypeScriptWatchAdapter,
    can_start_typescript,
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
        "if [ \"$1\" = \"--version\" ]; then\n"
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
    monkeypatch.delenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES", raising=False)
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
    monkeypatch.delenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS_TRUST_REPO_BINARIES", raising=False)
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


def test_unknown_project_coverage_never_reports_clean_fresh(tmp_path):
    source = tmp_path / "src" / "unknown.ts"
    source.parent.mkdir()
    source.write_text("const answer: number = 42;\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter._project_files = None
    version = file_version(source)

    adapter.notify_edit(source, version)
    adapter._complete_batch([], expected_errors=0)

    response = adapter.query(source, version, budget_ms=0)

    assert response.status == QueryStatus.FALLBACK_REQUIRED
    assert response.fallback_reason == "not_in_project_config"
