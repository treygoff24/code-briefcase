import json
import subprocess
import sys

def run_cli(args, payload=None):
    return subprocess.run(
        [sys.executable, "-m", "tldr.cli", *args],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        check=True,
    )


def test_claude_pre_read_cli_output_matches_current_schema(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": str(source)},
        "cwd": str(tmp_path),
    }

    result = run_cli(["hooks", "run", "pre-read", "--client", "claude"], payload)
    rendered = json.loads(result.stdout)

    assert rendered["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert rendered["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert rendered["hookSpecificOutput"]["updatedInput"]["limit"] == 200
    assert "additionalContext" in rendered["hookSpecificOutput"]


def test_codex_pre_edit_apply_patch_cli_output_matches_current_schema(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": "*** Begin Patch\n*** Update File: app.py\n@@\n def main():\n*** End Patch"
        },
        "cwd": str(tmp_path),
    }

    result = run_cli(["hooks", "run", "pre-edit", "--client", "codex"], payload)
    rendered = json.loads(result.stdout)

    assert rendered["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "main" in rendered["hookSpecificOutput"]["additionalContext"]
    assert "continue" not in rendered
    assert "suppressOutput" not in rendered


def test_hook_install_cli_can_target_temp_configs_without_user_config(tmp_path):
    claude_config = tmp_path / "claude-settings.json"
    codex_config = tmp_path / "codex-hooks.json"

    run_cli(["hooks", "install", "claude", "--config", str(claude_config)])
    run_cli(["hooks", "install", "codex", "--config", str(codex_config)])

    claude = json.loads(claude_config.read_text())
    codex = json.loads(codex_config.read_text())

    assert claude["hooks"]["PreToolUse"][0]["matcher"] == "Read"
    assert "hooks run pre-read" not in json.dumps(codex)
    assert codex["hooks"]["PreToolUse"][0]["matcher"] == "apply_patch|Edit|Write"
