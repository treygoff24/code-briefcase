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
        event_name="PreToolUse",
    )

    assert rendered["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert rendered["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert rendered["hookSpecificOutput"]["updatedInput"]["limit"] == 200
    assert rendered["hookSpecificOutput"]["additionalContext"] == "context"
    assert "systemMessage" not in rendered


def test_render_claude_post_tool_response_includes_event_name_and_context():
    rendered = render_hook_response(
        HookResponse(message="diagnostic", additional_context="diagnostic"),
        client="claude",
        event_name="PostToolUse",
    )

    assert rendered["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert rendered["hookSpecificOutput"]["additionalContext"] == "diagnostic"
    assert "systemMessage" not in rendered


def test_render_codex_pre_tool_response_uses_supported_context_shape():
    rendered = render_hook_response(
        HookResponse(message="context", additional_context="context", suppress_output=False),
        client="codex",
        event_name="PreToolUse",
    )

    json.dumps(rendered)
    assert rendered["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "additionalContext": "context",
    }
    assert "continue" not in rendered
    assert "suppressOutput" not in rendered
    assert "systemMessage" not in rendered


def test_render_codex_post_tool_response_uses_supported_context_shape():
    rendered = render_hook_response(
        HookResponse(message="diagnostic", additional_context="diagnostic"),
        client="codex",
        event_name="PostToolUse",
    )

    assert rendered["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert rendered["hookSpecificOutput"]["additionalContext"] == "diagnostic"


def test_render_codex_session_start_message_uses_hook_specific_context():
    rendered = render_hook_response(
        HookResponse(message="TLDR session hook: daemon start requested", suppress_output=True),
        client="codex",
        event_name="SessionStart",
    )

    assert rendered["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": "TLDR session hook: daemon start requested",
    }
    assert "continue" not in rendered
    assert "suppressOutput" not in rendered
    assert "systemMessage" not in rendered


def test_parse_codex_payload_with_tool_input(tmp_path):
    event = parse_hook_event(
        {"event": "preToolUse", "toolName": "Read", "toolInput": {"path": "app.py"}, "cwd": str(tmp_path)},
        client="codex",
    )

    assert event.event_name == "PreToolUse"
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

    assert render_hook_response(build_session_start_response(event).response, client="claude") == {}
