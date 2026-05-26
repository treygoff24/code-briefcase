from __future__ import annotations
from typing import Any

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from code_briefcase.hooks.runner import run_hook
from code_briefcase.telemetry import (
    record_hook_execution,
    record_watch_diagnostics_event,
    telemetry_path_hash,
    write_telemetry_record,
)


def test_telemetry_disabled_by_default(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.delenv("CODE_BRIEFCASE_TELEMETRY", raising=False)
    monkeypatch.setattr(
        "code_briefcase.telemetry.TELEMETRY_ENABLE_FILE", tmp_path / "missing.enabled"
    )
    monkeypatch.setenv(
        "CODE_BRIEFCASE_TELEMETRY_PATH", str(tmp_path / "telemetry.jsonl")
    )
    write_telemetry_record({"event": "test"})
    assert not (tmp_path / "telemetry.jsonl").exists()


def test_telemetry_writes_when_enabled(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(path))
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


def test_telemetry_can_be_enabled_by_global_flag_file(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.delenv("CODE_BRIEFCASE_TELEMETRY", raising=False)
    monkeypatch.setattr(
        "code_briefcase.telemetry.TELEMETRY_ENABLE_FILE", tmp_path / "telemetry.enabled"
    )
    monkeypatch.setenv(
        "CODE_BRIEFCASE_TELEMETRY_PATH", str(tmp_path / "telemetry.jsonl")
    )
    (tmp_path / "telemetry.enabled").write_text("1\n", encoding="utf-8")

    write_telemetry_record({"event": "test"})

    assert (tmp_path / "telemetry.jsonl").exists()


def test_env_can_disable_global_telemetry_flag_file(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "0")
    monkeypatch.setattr(
        "code_briefcase.telemetry.TELEMETRY_ENABLE_FILE", tmp_path / "telemetry.enabled"
    )
    monkeypatch.setenv(
        "CODE_BRIEFCASE_TELEMETRY_PATH", str(tmp_path / "telemetry.jsonl")
    )
    (tmp_path / "telemetry.enabled").write_text("1\n", encoding="utf-8")

    write_telemetry_record({"event": "test"})

    assert not (tmp_path / "telemetry.jsonl").exists()


def test_redacted_file_paths_keep_distinct_stable_hashes(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_REDACT_PATHS", "1")
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(telemetry_path))

    record_hook_execution(
        client="codex",
        hook_event="pre-read",
        project=tmp_path,
        duration_ms=1,
        status="ok",
        trigger_files=["src/app.py", "src/auth.py"],
    )

    payload = json.loads(telemetry_path.read_text(encoding="utf-8").splitlines()[-1])
    expected_app = f"<redacted>/{payload['project_hash']}/{telemetry_path_hash(tmp_path, 'src/app.py')}"
    expected_auth = f"<redacted>/{payload['project_hash']}/{telemetry_path_hash(tmp_path, 'src/auth.py')}"
    assert payload["trigger_files"] == [expected_app, expected_auth]
    assert expected_app != expected_auth


def test_unwritable_telemetry_path_is_swallowed(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    monkeypatch.setenv(
        "CODE_BRIEFCASE_TELEMETRY_PATH",
        str(tmp_path / "missing" / "nested" / "telemetry.jsonl"),
    )
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


def test_malformed_env_path_is_swallowed(monkeypatch: Any) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")

    def broken_path() -> Path:
        raise OSError("invalid telemetry path")

    monkeypatch.setattr("code_briefcase.telemetry.telemetry_path", broken_path)
    record_hook_execution(
        client="codex",
        hook_event="session-start",
        project=Path.cwd(),
        duration_ms=1,
        status="ok",
    )


def test_concurrent_hook_writes_produce_parseable_jsonl(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(path))

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


def test_hook_stdout_unchanged_with_telemetry(monkeypatch: Any, tmp_path: Any) -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "README.md"},
        "cwd": str(tmp_path),
    }
    monkeypatch.delenv("CODE_BRIEFCASE_TELEMETRY", raising=False)
    without = run_hook("pre-read", payload, client="claude")
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    monkeypatch.setenv(
        "CODE_BRIEFCASE_TELEMETRY_PATH", str(tmp_path / "telemetry.jsonl")
    )
    with_enabled = run_hook("pre-read", payload, client="claude")
    assert without == with_enabled


def test_statuses_are_distinguishable_in_telemetry(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(path))
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
    run_hook(
        "session-start",
        {"hook_event_name": "SessionStart", "cwd": str(tmp_path / "missing")},
        client="codex",
    )
    record_hook_execution(
        client="claude",
        hook_event="pre-read",
        project=tmp_path,
        duration_ms=1,
        status="error",
        error_kind="Timeout",
    )
    statuses = {
        json.loads(line)["status"]
        for line in path.read_text(encoding="utf-8").splitlines()
    }
    assert {"skipped", "ok", "error"} <= statuses


def test_telemetry_redacts_paths_by_default(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    monkeypatch.delenv("CODE_BRIEFCASE_TELEMETRY_REDACT_PATHS", raising=False)
    project = tmp_path / "secret-repo-name"
    project.mkdir()
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(telemetry_path))

    record_hook_execution(
        client="codex",
        hook_event="pre-read",
        project=project,
        duration_ms=1,
        status="ok",
        trigger_files=["src/app.py"],
        recommended_files=["src/auth.py"],
        surfaced_files=["src/auth.py"],
    )

    raw = telemetry_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert str(tmp_path) not in raw
    assert "secret-repo-name" not in raw
    assert "src/app.py" not in raw
    assert payload["project"] == f"<redacted>/{payload['project_hash']}"
    assert payload["trigger_files"][0].startswith(
        f"<redacted>/{payload['project_hash']}/"
    )


def test_hook_telemetry_records_candidate_lifecycle_without_content(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(telemetry_path))

    record_hook_execution(
        client="codex",
        hook_event="pre-edit",
        project=tmp_path,
        duration_ms=5,
        status="ok",
        trigger_files=["src/app.py"],
        recommended_files=["src/auth.py"],
        surfaced_files=["src/auth.py"],
        candidate_files=[
            {
                "path": "src/auth.py",
                "reason": "importer",
                "rank": 1,
                "score": 0.91,
                "surfaced": True,
            },
            {
                "path": "tests/test_auth.py",
                "reason": "test_neighbor",
                "rank": 2,
                "score": 0.74,
                "surfaced": False,
                "excluded_reason": "budget",
            },
        ],
        context_kind="edit_structure",
        hook_run_id="run-1",
    )

    raw = telemetry_path.read_text(encoding="utf-8")
    payload = json.loads(raw.splitlines()[-1])
    assert payload["schema_version"] == 2
    assert payload["hook_run_id"] == "run-1"
    assert payload["context_kind"] == "edit_structure"
    assert payload["candidate_files"][0]["path"].startswith(
        f"<redacted>/{payload['project_hash']}/"
    )
    assert payload["candidate_files"][0]["surfaced"] is True
    assert "def " not in raw
    assert "content" not in raw.lower()


def test_hook_telemetry_schema_v3_records_watch_diagnostics_fields(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(telemetry_path))

    record_hook_execution(
        client="codex",
        hook_event="post-edit",
        project=tmp_path,
        duration_ms=42,
        status="ok",
        watch_diagnostics_enabled=True,
        watch_diagnostics_attempted=True,
        watch_diagnostics_used=True,
        watch_diagnostics_status="fresh",
        watch_diagnostics_statuses=["fresh"],
        watch_diagnostics_age_ms=7,
        watch_diagnostics_wait_ms=11,
        watch_diagnostics_query_budget_ms=150,
        watch_diagnostics_batch_seq=3,
        diagnostics_backend="tsc-watch",
    )

    payload = json.loads(telemetry_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["watch_diagnostics_enabled"] is True
    assert payload["watch_diagnostics_used"] is True
    assert payload["watch_diagnostics_status"] == "fresh"
    assert payload["watch_diagnostics_statuses"] == ["fresh"]
    assert payload["watch_diagnostics_age_ms"] == 7
    assert payload["watch_diagnostics_wait_ms"] == 11
    assert payload["diagnostics_backend"] == "tsc-watch"


def test_watch_diagnostics_event_redacts_adapter_paths_by_default(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(telemetry_path))
    project = tmp_path / "secret-repo-name"
    project.mkdir()

    record_watch_diagnostics_event(
        project=project,
        action="recheck_complete",
        adapter_key=f"typescript|{project}/node_modules/.bin/tsc|{project}/tsconfig.json|noemit",
        duration_ms=33,
        status="fresh",
        batch_seq=2,
    )

    raw = telemetry_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["schema_version"] == 3
    assert payload["event"] == "watch-diagnostics-event"
    assert payload["action"] == "recheck_complete"
    assert payload["adapter_key_hash"]
    assert "secret-repo-name" not in raw
    assert "node_modules/.bin/tsc" not in raw


def test_local_rich_mode_records_raw_local_evidence_with_secret_hygiene(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_MODE", "local-rich")
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(telemetry_path))
    project = tmp_path / "repo"
    project.mkdir()

    record_hook_execution(
        client="codex",
        hook_event="pre-read",
        project=project,
        duration_ms=5,
        status="ok",
        trigger_files=["src/app.py"],
        recommended_files=["src/auth.py"],
        surfaced_files=["src/auth.py"],
        candidate_files=[
            {"path": "src/auth.py", "reason": "import", "rank": 1, "surfaced": True}
        ],
        tool_name="shell",
        tool_input={
            "command": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz rg -n login src/auth.py",
            "file_path": "src/app.py",
        },
    )

    raw = telemetry_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["telemetry_mode"] == "local-rich"
    assert payload["project"] == str(project.resolve())
    assert payload["trigger_files"] == ["src/app.py"]
    assert payload["candidate_files"][0]["path"] == "src/auth.py"
    assert payload["local_evidence"]["tool_name"] == "shell"
    assert (
        "rg -n login src/auth.py" in payload["local_evidence"]["tool_input"]["command"]
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in raw
    assert "OPENAI_API_KEY=[redacted]" in raw
    assert payload["local_evidence"]["raw_candidate_files"][0]["path"] == "src/auth.py"
    assert payload["local_evidence"]["raw_candidate_files"][0]["path_hash"]


def test_local_rich_mode_redacts_secret_like_paths_everywhere(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_MODE", "local-rich")
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(telemetry_path))

    record_hook_execution(
        client="codex",
        hook_event="pre-read",
        project=tmp_path,
        duration_ms=1,
        status="skipped",
        trigger_files=[".env.py", "src/secret_config.py"],
        recommended_files=["secrets/config.py", "src/api_credentials.py"],
        surfaced_files=["credentials/client.py"],
        candidate_files=[
            {
                "path": "secrets/config.py",
                "reason": "import",
                "rank": 1,
                "surfaced": True,
            },
            {
                "path": "src/api_credentials.py",
                "reason": "import",
                "rank": 2,
                "surfaced": False,
            },
        ],
        tool_name="Read",
        tool_input={"file_path": "src/secret_config.py"},
    )

    raw = telemetry_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert ".env.py" not in raw
    assert "src/secret_config.py" not in raw
    assert "secrets/config.py" not in raw
    assert "src/api_credentials.py" not in raw
    assert "credentials/client.py" not in raw
    assert payload["trigger_files"] == [
        "[redacted-secret-path]",
        "[redacted-secret-path]",
    ]
    assert payload["recommended_related_files"] == [
        "[redacted-secret-path]",
        "[redacted-secret-path]",
    ]
    assert payload["surfaced_files"] == ["[redacted-secret-path]"]
    assert payload["candidate_files"][0]["path"] == "[redacted-secret-path]"
    assert (
        payload["local_evidence"]["tool_input"]["file_path"] == "[redacted-secret-path]"
    )
    assert payload["local_evidence"]["raw_trigger_files"] == [
        "[redacted-secret-path]",
        "[redacted-secret-path]",
    ]
    assert (
        payload["local_evidence"]["raw_candidate_files"][0]["path"]
        == "[redacted-secret-path]"
    )
    assert (
        payload["local_evidence"]["raw_candidate_files"][1]["path"]
        == "[redacted-secret-path]"
    )


def test_runner_passes_tool_input_to_local_rich_telemetry(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_MODE", "local-rich")
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(telemetry_path))
    (tmp_path / "README.md").write_text("# hello\n", encoding="utf-8")

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

    payload = json.loads(telemetry_path.read_text(encoding="utf-8"))
    assert payload["local_evidence"]["tool_name"] == "Read"
    assert payload["local_evidence"]["tool_input"]["file_path"] == "README.md"


def test_cli_hook_emits_telemetry(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY", "1")
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("CODE_BRIEFCASE_TELEMETRY_PATH", str(telemetry_path))
    (tmp_path / "README.md").write_text("# hello\n", encoding="utf-8")
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "README.md"},
        "cwd": str(tmp_path),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_briefcase.cli",
            "hooks",
            "run",
            "pre-read",
            "--client",
            "codex",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    lines = telemetry_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    json.loads(lines[-1])
