from typing import Any
import json
import subprocess
import sys


def run_cli(args: Any, input_text: Any = "") -> Any:
    return subprocess.run(
        [sys.executable, "-m", "code_briefcase.cli", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )


def test_tldr_pack_json_on_temp_file_returns_json(tmp_path: Any) -> None:
    (tmp_path / "app.py").write_text("def main():\n    return 1\n")

    result = run_cli(
        ["pack", "main", "--project", str(tmp_path), "--file", "app.py", "--json"]
    )

    payload = json.loads(result.stdout)
    assert payload["items"][0]["path"] == "app.py"


def test_hooks_run_session_start_reads_stdin_and_returns_json_noop() -> None:
    payload = {"hook_event_name": "SessionStart", "cwd": "/does/not/exist"}

    result = run_cli(
        ["hooks", "run", "session-start", "--client", "claude"], json.dumps(payload)
    )

    assert json.loads(result.stdout) == {}


def test_hooks_run_pre_read_reads_stdin_and_returns_json(tmp_path: Any) -> None:
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "app.py"},
        "cwd": str(tmp_path),
    }

    result = run_cli(
        ["hooks", "run", "pre-read", "--client", "claude"], json.dumps(payload)
    )
    rendered = json.loads(result.stdout)

    assert rendered["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_noop_hook_returns_empty_json() -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "x.md"},
    }

    result = run_cli(
        ["hooks", "run", "pre-read", "--client", "claude"], json.dumps(payload)
    )

    assert json.loads(result.stdout) == {}
