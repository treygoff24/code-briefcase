import json
import subprocess
import sys

def run_cli(args, payload=None):
    return subprocess.run(
        [sys.executable, "-m", "code_briefcase.cli", *args],
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

    assert rendered == {}
    assert "hookSpecificOutput" not in rendered


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


# --- Phase 1: Runtime CLI shape additions ---


def test_droid_client_accepted_in_hooks_run(tmp_path):
    payload = {
        "hook_event_name": "SessionStart",
        "cwd": str(tmp_path),
    }
    result = run_cli(["hooks", "run", "session-start", "--client", "droid"], payload)
    rendered = json.loads(result.stdout)
    # Droid SessionStart may produce context output; verify it uses Droid shape
    if rendered:
        assert "hookSpecificOutput" in rendered
        assert rendered["hookSpecificOutput"].get("hookEventName") == "SessionStart"


def test_factory_client_accepted_in_hooks_run(tmp_path):
    payload = {
        "hook_event_name": "SessionStart",
        "cwd": str(tmp_path),
    }
    result = run_cli(["hooks", "run", "session-start", "--client", "factory"], payload)
    rendered = json.loads(result.stdout)
    # Factory SessionStart may produce context output; verify it uses Factory/Droid shape
    if rendered:
        assert "hookSpecificOutput" in rendered
        assert rendered["hookSpecificOutput"].get("hookEventName") == "SessionStart"


def test_opencode_client_accepted_in_hooks_run(tmp_path):
    payload = {
        "hook_event_name": "SessionStart",
        "cwd": str(tmp_path),
    }
    result = run_cli(["hooks", "run", "session-start", "--client", "opencode"], payload)
    rendered = json.loads(result.stdout)
    # OpenCode adapter consumes Code Briefcase-internal JSON and mutates OpenCode callback
    # output where the plugin API supports it.
    assert rendered["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_user_prompt_submit_event_accepted_in_hooks_run():
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Hello world",
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "user-prompt-submit", "--client", "codex"], payload)
    rendered = json.loads(result.stdout)
    # Clean prompt should noop
    assert rendered == {}


def test_permission_request_event_accepted_in_hooks_run():
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "npm test"},
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "permission-request", "--client", "codex"], payload)
    rendered = json.loads(result.stdout)
    # Safe command should noop
    assert rendered == {}


def test_stop_event_accepted_in_hooks_run():
    payload = {
        "hook_event_name": "Stop",
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "stop", "--client", "codex"], payload)
    rendered = json.loads(result.stdout)
    assert rendered == {}


def test_session_end_event_accepted_in_hooks_run():
    payload = {
        "hook_event_name": "SessionEnd",
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "session-end", "--client", "droid"], payload)
    rendered = json.loads(result.stdout)
    assert rendered == {}


def test_notification_event_accepted_in_hooks_run():
    payload = {
        "hook_event_name": "Notification",
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "notification", "--client", "droid"], payload)
    rendered = json.loads(result.stdout)
    assert rendered == {}


def test_pre_tool_event_accepted_in_hooks_run():
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "npm test"},
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "pre-tool", "--client", "codex"], payload)
    rendered = json.loads(result.stdout)
    # Not a destructive command, should noop
    assert rendered == {}


def test_post_tool_event_accepted_in_hooks_run():
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "npm test"},
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "post-tool", "--client", "codex"], payload)
    rendered = json.loads(result.stdout)
    assert rendered == {}


def test_subagent_start_event_accepted_in_hooks_run():
    payload = {
        "hook_event_name": "SubagentStart",
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "subagent-start", "--client", "droid"], payload)
    rendered = json.loads(result.stdout)
    assert rendered == {}


def test_subagent_stop_event_accepted_in_hooks_run():
    payload = {
        "hook_event_name": "SubagentStop",
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "subagent-stop", "--client", "droid"], payload)
    rendered = json.loads(result.stdout)
    assert rendered == {}


def test_pre_compact_event_accepted_in_hooks_run(tmp_path):
    (tmp_path / "app.py").write_text("def main():\n    return 1\n")
    payload = {
        "hook_event_name": "PreCompact",
        "cwd": str(tmp_path),
    }
    result = run_cli(["hooks", "run", "pre-compact", "--client", "droid"], payload)
    rendered = json.loads(result.stdout)
    assert rendered["hookSpecificOutput"]["hookEventName"] == "PreCompact"
    assert "Code Briefcase compact context" in rendered["hookSpecificOutput"]["additionalContext"]


def test_codex_permission_request_cli_blocks_destructive():
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"},
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "permission-request", "--client", "codex"], payload)
    rendered = json.loads(result.stdout)
    assert "hookSpecificOutput" in rendered
    assert rendered["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_droid_user_prompt_submit_cli_blocks_secret():
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Here is my key: sk-" + "A" * 48,
        "cwd": "/tmp",
    }
    result = run_cli(["hooks", "run", "user-prompt-submit", "--client", "droid"], payload)
    rendered = json.loads(result.stdout)
    assert rendered["decision"] == "block"
    assert "possible OpenAI API key" in rendered["reason"]
