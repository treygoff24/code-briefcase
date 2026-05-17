import json

from tldr.hooks.runtime import HookResponse, parse_hook_event, render_hook_response
from tldr.hooks.session import build_session_start_response


def test_parse_claude_tool_event(tmp_path):
    event = parse_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "app.py"},
            "cwd": str(tmp_path),
            "session_id": "abc",
        },
        client="claude",
    )

    assert event.client == "claude"
    assert event.event_name == "PreToolUse"
    assert event.tool_name == "Read"
    assert event.tool_input["file_path"] == "app.py"
    assert event.cwd == tmp_path


def test_render_noop_is_empty():
    assert render_hook_response(HookResponse.noop(), client="claude") == {}


def test_render_claude_pre_tool_response_includes_specific_output():
    rendered = render_hook_response(
        HookResponse(
            permission_decision="allow",
            updated_input={"file_path": "app.py", "limit": 200},
            additional_context="context",
        ),
        client="claude",
    )

    assert rendered["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert rendered["hookSpecificOutput"]["updatedInput"]["limit"] == 200
    assert rendered["hookSpecificOutput"]["additionalContext"] == "context"


def test_render_codex_response_is_conservative_json():
    rendered = render_hook_response(HookResponse(message="hello"), client="codex")

    json.dumps(rendered)
    assert rendered["systemMessage"] == "hello"
    assert "hookSpecificOutput" not in rendered


def test_parse_codex_payload_with_tool_input(tmp_path):
    event = parse_hook_event(
        {"event": "preToolUse", "toolName": "Read", "toolInput": {"path": "app.py"}, "cwd": str(tmp_path)},
        client="codex",
    )

    assert event.tool_name == "Read"
    assert event.tool_input["path"] == "app.py"


def test_parse_codex_payload_with_tool_response_file_path(tmp_path):
    event = parse_hook_event(
        {
            "event": "postToolUse",
            "toolName": "Edit",
            "toolResponse": {"filePath": "app.py"},
            "cwd": str(tmp_path),
        },
        client="codex",
    )

    assert event.tool_result["filePath"] == "app.py"
    assert event.raw["toolResponse"]["filePath"] == "app.py"


def test_session_start_noop_can_render_for_missing_project(tmp_path):
    event = parse_hook_event({"hook_event_name": "SessionStart", "cwd": str(tmp_path / "missing")})

    assert render_hook_response(build_session_start_response(event), client="claude") == {}
