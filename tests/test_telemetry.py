from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tldr.hooks.runner import run_hook
from tldr.telemetry import record_hook_execution, write_telemetry_record


def test_telemetry_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("TLDR_TELEMETRY", raising=False)
    monkeypatch.setenv("TLDR_TELEMETRY_PATH", str(tmp_path / "telemetry.jsonl"))
    write_telemetry_record({"event": "test"})
    assert not (tmp_path / "telemetry.jsonl").exists()


def test_telemetry_writes_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("TLDR_TELEMETRY", "1")
    path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("TLDR_TELEMETRY_PATH", str(path))
    record_hook_execution(
        client="codex",
        hook_event="session-start",
        project=tmp_path,
        duration_ms=10,
        status="ok",
        injected_bytes=12,
    )
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "ok"
    assert payload["injected_bytes"] == 12
    assert "snippet" not in payload


def test_unwritable_telemetry_path_is_swallowed(monkeypatch, tmp_path):
    monkeypatch.setenv("TLDR_TELEMETRY", "1")
    monkeypatch.setenv("TLDR_TELEMETRY_PATH", str(tmp_path / "missing" / "nested" / "telemetry.jsonl"))
    os.chmod(tmp_path, 0o500)
    try:
        record_hook_execution(
            client="codex",
            hook_event="session-start",
            project=tmp_path,
            duration_ms=1,
            status="ok",
        )
    finally:
        os.chmod(tmp_path, 0o700)


def test_malformed_env_path_is_swallowed(monkeypatch):
    monkeypatch.setenv("TLDR_TELEMETRY", "1")

    def broken_path() -> Path:
        raise OSError("invalid telemetry path")

    monkeypatch.setattr("tldr.telemetry.telemetry_path", broken_path)
    record_hook_execution(
        client="codex",
        hook_event="session-start",
        project=Path.cwd(),
        duration_ms=1,
        status="ok",
    )


def test_concurrent_hook_writes_produce_parseable_jsonl(monkeypatch, tmp_path):
    monkeypatch.setenv("TLDR_TELEMETRY", "1")
    path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("TLDR_TELEMETRY_PATH", str(path))

    def write_one(index: int) -> None:
        record_hook_execution(
            client="codex",
            hook_event=f"event-{index}",
            project=tmp_path,
            duration_ms=index,
            status="ok",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_one, range(20)))

    for line in path.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_hook_stdout_unchanged_with_telemetry(monkeypatch, tmp_path):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "README.md"},
        "cwd": str(tmp_path),
    }
    monkeypatch.delenv("TLDR_TELEMETRY", raising=False)
    without = run_hook("pre-read", payload, client="claude")
    monkeypatch.setenv("TLDR_TELEMETRY", "1")
    monkeypatch.setenv("TLDR_TELEMETRY_PATH", str(tmp_path / "telemetry.jsonl"))
    with_enabled = run_hook("pre-read", payload, client="claude")
    assert without == with_enabled


def test_statuses_are_distinguishable_in_telemetry(monkeypatch, tmp_path):
    monkeypatch.setenv("TLDR_TELEMETRY", "1")
    path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("TLDR_TELEMETRY_PATH", str(path))
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)

    run_hook(
        "pre-read",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
            "cwd": str(tmp_path),
        },
        client="claude",
    )
    run_hook(
        "pre-read",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "app.py"},
            "cwd": str(tmp_path),
        },
        client="claude",
    )
    run_hook("session-start", {"hook_event_name": "SessionStart", "cwd": str(tmp_path / "missing")}, client="codex")
    record_hook_execution(
        client="claude",
        hook_event="pre-read",
        project=tmp_path,
        duration_ms=1,
        status="error",
        error_kind="Timeout",
    )
    statuses = {json.loads(line)["status"] for line in path.read_text(encoding="utf-8").splitlines()}
    assert {"skipped", "ok", "error"} <= statuses


def test_cli_hook_emits_telemetry(monkeypatch, tmp_path):
    monkeypatch.setenv("TLDR_TELEMETRY", "1")
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("TLDR_TELEMETRY_PATH", str(telemetry_path))
    payload = {"hook_event_name": "SessionStart", "cwd": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "tldr.cli", "hooks", "run", "session-start", "--client", "codex"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    lines = telemetry_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    json.loads(lines[-1])
