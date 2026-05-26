from typing import Any
import json
import sys
from pathlib import Path
import pytest

from code_briefcase.hooks.runtime import (
    HookResponse,
    parse_hook_event,
    render_hook_response,
)
from code_briefcase.hooks.session import build_session_start_response


def test_parse_claude_tool_event(tmp_path: Any) -> None:
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


def test_render_noop_is_empty() -> None:
    assert render_hook_response(HookResponse.noop(), client="claude") == {}


def test_render_claude_pre_tool_response_includes_specific_output() -> None:
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


def test_render_claude_post_tool_response_includes_event_name_and_context() -> None:
    rendered = render_hook_response(
        HookResponse(message="diagnostic", additional_context="diagnostic"),
        client="claude",
        event_name="PostToolUse",
    )

    assert rendered["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert rendered["hookSpecificOutput"]["additionalContext"] == "diagnostic"
    assert "systemMessage" not in rendered


def test_render_codex_pre_tool_response_uses_supported_context_shape() -> None:
    rendered = render_hook_response(
        HookResponse(
            message="context", additional_context="context", suppress_output=False
        ),
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


def test_render_codex_post_tool_response_uses_supported_context_shape() -> None:
    rendered = render_hook_response(
        HookResponse(message="diagnostic", additional_context="diagnostic"),
        client="codex",
        event_name="PostToolUse",
    )

    assert rendered["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert rendered["hookSpecificOutput"]["additionalContext"] == "diagnostic"


def test_render_codex_session_start_message_uses_hook_specific_context() -> None:
    rendered = render_hook_response(
        HookResponse(
            message="Code Briefcase session hook: daemon start requested",
            suppress_output=True,
        ),
        client="codex",
        event_name="SessionStart",
    )

    assert rendered["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": "Code Briefcase session hook: daemon start requested",
    }
    assert "continue" not in rendered
    assert "suppressOutput" not in rendered
    assert "systemMessage" not in rendered


def test_parse_codex_payload_with_tool_input(tmp_path: Any) -> None:
    event = parse_hook_event(
        {
            "event": "preToolUse",
            "toolName": "Read",
            "toolInput": {"path": "app.py"},
            "cwd": str(tmp_path),
        },
        client="codex",
    )

    assert event.event_name == "PreToolUse"
    assert event.tool_name == "Read"
    assert event.tool_input["path"] == "app.py"


def test_parse_codex_payload_with_tool_response_file_path(tmp_path: Any) -> None:
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


def test_session_start_noop_can_render_for_missing_project(tmp_path: Any) -> None:
    event = parse_hook_event(
        {"hook_event_name": "SessionStart", "cwd": str(tmp_path / "missing")}
    )

    assert (
        render_hook_response(
            build_session_start_response(event).response, client="claude"
        )
        == {}
    )


# --- Phase 0 Contract Tests ---


@pytest.mark.parametrize(
    "fixture_name,expected",
    [
        pytest.param(
            "codex_session_start.json",
            {
                "client": "codex",
                "event_name": "SessionStart",
                "tool_name": None,
                "tool_input": {},
                "tool_result": {},
                "session_id": "codex-session-123",
            },
            id="codex_session_start",
        ),
        pytest.param(
            "codex_pretooluse_apply_patch.json",
            {
                "client": "codex",
                "event_name": "PreToolUse",
                "tool_name": "apply_patch",
                "tool_input": {"patch": "diff --git a/file.py b/file.py\n..."},
                "tool_result": {},
                "session_id": "codex-session-123",
            },
            id="codex_pretooluse_apply_patch",
        ),
        pytest.param(
            "codex_permission_request_bash.json",
            {
                "client": "codex",
                "event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "tool_result": {},
                "session_id": "codex-session-123",
            },
            id="codex_permission_request_bash",
        ),
        pytest.param(
            "codex_posttooluse_apply_patch.json",
            {
                "client": "codex",
                "event_name": "PostToolUse",
                "tool_name": "apply_patch",
                "tool_input": {"patch": "diff --git " + "a/file.py b/file.py\n..."},
                "tool_result": {"status": "success"},
                "session_id": "codex-session-123",
            },
            id="codex_posttooluse_apply_patch",
        ),
        pytest.param(
            "codex_user_prompt_submit.json",
            {
                "client": "codex",
                "event_name": "UserPromptSubmit",
                "tool_name": None,
                "tool_input": {},
                "tool_result": {},
                "session_id": "codex-session-123",
            },
            id="codex_user_prompt_submit",
        ),
        pytest.param(
            "codex_stop.json",
            {
                "client": "codex",
                "event_name": "Stop",
                "tool_name": None,
                "tool_input": {},
                "tool_result": {},
                "session_id": "codex-session-123",
            },
            id="codex_stop",
        ),
        pytest.param(
            "droid_session_start.json",
            {
                "client": "droid",
                "event_name": "SessionStart",
                "tool_name": None,
                "tool_input": {},
                "tool_result": {},
                "session_id": "droid-session-abc",
            },
            id="droid_session_start",
        ),
        pytest.param(
            "droid_pretooluse_read.json",
            {
                "client": "droid",
                "event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {
                    "file_path": "/Users/treygoff/Code/code-briefcase/code_briefcase/hooks/runtime.py"
                },
                "tool_result": {},
                "session_id": "droid-session-abc",
            },
            id="droid_pretooluse_read",
        ),
        pytest.param(
            "droid_pretooluse_edit.json",
            {
                "client": "droid",
                "event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "/Users/treygoff/Code/code-briefcase/code_briefcase/hooks/runtime.py",
                    "old_str": "foo",
                    "new_str": "bar",
                },
                "tool_result": {},
                "session_id": "droid-session-abc",
            },
            id="droid_pretooluse_edit",
        ),
        pytest.param(
            "droid_pretooluse_create.json",
            {
                "client": "droid",
                "event_name": "PreToolUse",
                "tool_name": "Create",
                "tool_input": {
                    "file_path": "/Users/treygoff/Code/code-briefcase/code_briefcase/hooks/new_file.py",
                    "content": "print('hello')",
                },
                "tool_result": {},
                "session_id": "droid-session-abc",
            },
            id="droid_pretooluse_create",
        ),
        pytest.param(
            "droid_pretooluse_apply_patch.json",
            {
                "client": "droid",
                "event_name": "PreToolUse",
                "tool_name": "ApplyPatch",
                "tool_input": {"patch": "some diff"},
                "tool_result": {},
                "session_id": "droid-session-abc",
            },
            id="droid_pretooluse_apply_patch",
        ),
        pytest.param(
            "droid_pretooluse_execute.json",
            {
                "client": "droid",
                "event_name": "PreToolUse",
                "tool_name": "Execute",
                "tool_input": {"command": "rm -rf /"},
                "tool_result": {},
                "session_id": "droid-session-abc",
            },
            id="droid_pretooluse_execute",
        ),
        pytest.param(
            "droid_posttooluse_create.json",
            {
                "client": "droid",
                "event_name": "PostToolUse",
                "tool_name": "Create",
                "tool_input": {
                    "file_path": "/Users/treygoff/Code/code-briefcase/code_briefcase/hooks/new_file.py",
                    "content": "print('hello')",
                },
                "tool_result": {"status": "success"},
                "session_id": "droid-session-abc",
            },
            id="droid_posttooluse_create",
        ),
        pytest.param(
            "droid_user_prompt_submit.json",
            {
                "client": "droid",
                "event_name": "UserPromptSubmit",
                "tool_name": None,
                "tool_input": {},
                "tool_result": {},
                "session_id": "droid-session-abc",
            },
            id="droid_user_prompt_submit",
        ),
        pytest.param(
            "droid_precompact_manual.json",
            {
                "client": "droid",
                "event_name": "PreCompact",
                "tool_name": None,
                "tool_input": {},
                "tool_result": {},
                "session_id": "droid-session-abc",
            },
            id="droid_precompact_manual",
        ),
        pytest.param(
            "droid_stop.json",
            {
                "client": "droid",
                "event_name": "Stop",
                "tool_name": None,
                "tool_input": {},
                "tool_result": {},
                "session_id": "droid-session-abc",
            },
            id="droid_stop",
        ),
        pytest.param(
            "droid_session_end.json",
            {
                "client": "droid",
                "event_name": "SessionEnd",
                "tool_name": None,
                "tool_input": {},
                "tool_result": {},
                "session_id": "droid-session-abc",
            },
            id="droid_session_end",
        ),
        pytest.param(
            "opencode_session_created.json",
            {
                "client": "opencode",
                "event_name": "SessionStart",
                "tool_name": None,
                "tool_input": {},
                "tool_result": {},
                "session_id": "opencode-session-456",
            },
            id="opencode_session_created",
        ),
        pytest.param(
            "opencode_tool_execute_before_edit.json",
            {
                "client": "opencode",
                "event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": "app.py"},
                "tool_result": {},
                "session_id": "opencode-session-456",
            },
            id="opencode_tool_execute_before_edit",
        ),
        pytest.param(
            "opencode_tool_execute_after_edit.json",
            {
                "client": "opencode",
                "event_name": "PostToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": "app.py"},
                "tool_result": {"status": "success"},
                "session_id": "opencode-session-456",
            },
            id="opencode_tool_execute_after_edit",
        ),
        pytest.param(
            "opencode_permission_asked.json",
            {
                "client": "opencode",
                "event_name": "PermissionRequest",
                "tool_name": "Execute",
                "tool_input": {"command": "rm -rf /"},
                "tool_result": {},
                "session_id": "opencode-session-456",
            },
            id="opencode_permission_asked",
        ),
        pytest.param(
            "opencode_file_edited.json",
            {
                "client": "opencode",
                "event_name": "PostToolUse",
                "tool_name": "file.edited",
                "tool_input": {"file_path": "app.py"},
                "tool_result": {},
                "session_id": "opencode-session-456",
            },
            id="opencode_file_edited",
        ),
    ],
)
def test_fixture_normalization(fixture_name: Any, expected: Any) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "hooks" / fixture_name
    with open(fixture_path) as f:
        payload = json.load(f)

    client_arg = fixture_name.split("_")[0]
    event = parse_hook_event(payload, client=client_arg)

    assert event.client == expected["client"]
    assert event.event_name == expected["event_name"]
    assert event.tool_name == expected["tool_name"]
    assert event.tool_input == expected["tool_input"]
    assert event.tool_result == expected["tool_result"]
    assert event.session_id == expected["session_id"]
    assert event.raw == payload


def test_negative_render_forbidden_fields_codex() -> None:
    response = HookResponse(additional_context="some context")
    rendered = render_hook_response(response, client="codex", event_name="PreToolUse")
    # Forbidden in Codex PreToolUse: continue, stopReason, suppressOutput, updatedPermissions
    assert "continue" not in rendered
    assert "suppressOutput" not in rendered
    assert "updatedPermissions" not in rendered
    assert "stopReason" not in rendered


def test_negative_render_forbidden_fields_codex_permission_request() -> None:
    response_perm = HookResponse(permission_decision="deny", message="blocked")
    rendered_perm = render_hook_response(
        response_perm, client="codex", event_name="PermissionRequest"
    )
    # Forbidden in Codex PermissionRequest: updatedInput, updatedPermissions, interrupt, generic permissionDecision
    hook_output = rendered_perm.get("hookSpecificOutput", {})
    assert "updatedInput" not in hook_output
    assert "updatedPermissions" not in hook_output
    assert "interrupt" not in hook_output
    assert "permissionDecision" not in hook_output


@pytest.mark.parametrize(
    "event_name,response,expected",
    [
        pytest.param(
            "SessionStart",
            HookResponse(additional_context="some context"),
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "some context",
                }
            },
            id="droid_session_start_with_context",
        ),
        pytest.param("SessionStart", HookResponse(), {}, id="droid_session_start_noop"),
        pytest.param(
            "PreToolUse",
            HookResponse(
                permission_decision="deny", reason="destructive command blocked"
            ),
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "destructive command blocked",
                }
            },
            id="droid_pre_tool_deny",
        ),
        pytest.param(
            "PostToolUse",
            HookResponse(additional_context="diagnostics context"),
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "diagnostics context",
                }
            },
            id="droid_post_tool_diagnostics",
        ),
        pytest.param(
            "UserPromptSubmit",
            HookResponse(
                decision="block",
                reason="possible OpenAI API key",
                additional_context="Some diagnostic warning",
            ),
            {
                "decision": "block",
                "reason": "possible OpenAI API key",
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "Some diagnostic warning",
                },
            },
            id="droid_user_prompt_block",
        ),
        pytest.param("Stop", HookResponse(), {}, id="droid_stop_noop"),
        pytest.param(
            "PreCompact",
            HookResponse(additional_context="compact context"),
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreCompact",
                    "additionalContext": "compact context",
                }
            },
            id="droid_pre_compact_context",
        ),
        pytest.param(
            "Stop",
            HookResponse(message="blocked stop"),
            {},
            id="droid_stop_with_loop_prevention",
        ),
    ],
)
def test_droid_renderer_matrix(event_name: Any, response: Any, expected: Any) -> None:
    rendered = render_hook_response(response, client="droid", event_name=event_name)
    assert rendered == expected


def test_droid_stop_loop_prevention_payload(monkeypatch: Any) -> None:
    """Stop/SubagentStop with stop_hook_active in raw payload must emit {} and never return decision=block."""
    payload = {
        "event": "Stop",
        "hook_event_name": "Stop",
        "sessionId": "droid-session-abc",
        "stop_hook_active": True,
    }
    response = HookResponse(additional_context="should be ignored")
    rendered = render_hook_response(
        response, client="droid", event_name="Stop", raw_payload=payload
    )
    assert rendered == {}
    assert "decision" not in rendered


def test_droid_stop_loop_prevention_env_var(monkeypatch: Any) -> None:
    """Stop/SubagentStop with CODE_BRIEFCASE_STOP_HOOK_ACTIVE env var must emit {} and never return decision=block."""
    monkeypatch.setenv("CODE_BRIEFCASE_STOP_HOOK_ACTIVE", "1")
    response = HookResponse(additional_context="should be ignored")
    rendered = render_hook_response(response, client="droid", event_name="Stop")
    assert rendered == {}
    assert "decision" not in rendered


def test_runner_exit_behavior_noop(monkeypatch: Any, capsys: Any) -> None:
    import io
    from code_briefcase.hooks.runner import run_hook_from_stdin

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    exit_code = run_hook_from_stdin("SessionStart", client="codex")
    assert exit_code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {}


def test_runner_exit_behavior_fallback_blocking(monkeypatch: Any, capsys: Any) -> None:
    """Generic client returns exit 2 for blocking decisions where JSON control is not available."""
    import io
    from code_briefcase.hooks import runner
    from code_briefcase.hooks.outcome import HookExecutionResult

    res = HookExecutionResult(
        status="ok",
        response=HookResponse(
            permission_decision="deny", reason="blocked by tool-guard"
        ),
    )
    monkeypatch.setattr(runner, "_dispatch", lambda *args, **kwargs: res)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    exit_code = runner.run_hook_from_stdin("PreToolUse", client="generic")
    assert exit_code == 2
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- Phase 1: Codex output matrix tests ---


class TestCodexPermissionRequestRendering:
    def test_codex_permission_request_deny_uses_decision_behavior(self) -> None:
        response = HookResponse(
            permission_decision="deny", reason="destructive command"
        )
        rendered = render_hook_response(
            response, client="codex", event_name="PermissionRequest"
        )
        assert "hookSpecificOutput" in rendered
        decision = rendered["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "deny"
        assert decision["message"] == "destructive command"
        # Forbidden fields
        assert "permissionDecision" not in rendered["hookSpecificOutput"]
        assert "updatedInput" not in rendered["hookSpecificOutput"]
        assert "updatedPermissions" not in rendered["hookSpecificOutput"]
        assert "interrupt" not in rendered["hookSpecificOutput"]

    def test_codex_permission_request_block_uses_decision_behavior(self) -> None:
        response = HookResponse(decision="block", reason="security risk")
        rendered = render_hook_response(
            response, client="codex", event_name="PermissionRequest"
        )
        assert rendered["hookSpecificOutput"]["decision"]["behavior"] == "deny"
        assert rendered["hookSpecificOutput"]["decision"]["message"] == "security risk"

    def test_codex_permission_request_noop_abstains(self) -> None:
        response = HookResponse()
        rendered = render_hook_response(
            response, client="codex", event_name="PermissionRequest"
        )
        assert rendered == {}


class TestCodexUserPromptSubmitRendering:
    def test_codex_user_prompt_submit_block(self) -> None:
        response = HookResponse(decision="block", reason="possible OpenAI API key")
        rendered = render_hook_response(
            response, client="codex", event_name="UserPromptSubmit"
        )
        assert rendered["decision"] == "block"
        assert rendered["reason"] == "possible OpenAI API key"

    def test_codex_user_prompt_submit_block_with_context(self) -> None:
        response = HookResponse(
            decision="block", reason="secret found", additional_context="warning"
        )
        rendered = render_hook_response(
            response, client="codex", event_name="UserPromptSubmit"
        )
        assert rendered["decision"] == "block"
        assert rendered["hookSpecificOutput"]["additionalContext"] == "warning"

    def test_codex_user_prompt_submit_context_only(self) -> None:
        response = HookResponse(additional_context="some workspace info")
        rendered = render_hook_response(
            response, client="codex", event_name="UserPromptSubmit"
        )
        assert rendered["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert (
            rendered["hookSpecificOutput"]["additionalContext"] == "some workspace info"
        )


class TestCodexStopRendering:
    def test_codex_stop_always_noop(self) -> None:
        response = HookResponse(additional_context="should be ignored")
        rendered = render_hook_response(response, client="codex", event_name="Stop")
        assert rendered == {}


class TestCodexPreToolUseDenyReason:
    def test_codex_pre_tool_deny_includes_reason(self) -> None:
        response = HookResponse(
            permission_decision="deny", reason="destructive command blocked"
        )
        rendered = render_hook_response(
            response, client="codex", event_name="PreToolUse"
        )
        assert rendered["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert (
            rendered["hookSpecificOutput"]["permissionDecisionReason"]
            == "destructive command blocked"
        )


# --- Phase 1: Droid additional tests ---


class TestDroidSessionEndNotification:
    def test_droid_session_end_noop(self) -> None:
        response = HookResponse(additional_context="should be ignored")
        rendered = render_hook_response(
            response, client="droid", event_name="SessionEnd"
        )
        assert rendered == {}

    def test_droid_notification_noop(self) -> None:
        response = HookResponse(additional_context="should be ignored")
        rendered = render_hook_response(
            response, client="droid", event_name="Notification"
        )
        assert rendered == {}

    def test_droid_permission_request_deny(self) -> None:
        response = HookResponse(
            permission_decision="deny", reason="destructive command"
        )
        rendered = render_hook_response(
            response, client="droid", event_name="PermissionRequest"
        )
        assert rendered == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "permissionDecision": "deny",
                "permissionDecisionReason": "destructive command",
            }
        }


class TestDroidSubagentStopLoopPrevention:
    def test_droid_subagent_stop_noop(self) -> None:
        response = HookResponse(additional_context="should be ignored")
        rendered = render_hook_response(
            response, client="droid", event_name="SubagentStop"
        )
        assert rendered == {}

    def test_droid_subagent_stop_loop_prevention_payload(self) -> None:
        payload = {"event": "SubagentStop", "stop_hook_active": True}
        response = HookResponse(additional_context="should be ignored")
        rendered = render_hook_response(
            response, client="droid", event_name="SubagentStop", raw_payload=payload
        )
        assert rendered == {}

    def test_droid_subagent_stop_loop_prevention_env_var(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CODE_BRIEFCASE_STOP_HOOK_ACTIVE", "1")
        response = HookResponse(additional_context="should be ignored")
        rendered = render_hook_response(
            response, client="droid", event_name="SubagentStop"
        )
        assert rendered == {}


# --- Phase 1: OpenCode rendering tests ---


class TestOpenCodeRendering:
    def test_opencode_context_is_adapter_internal_json(self) -> None:
        response = HookResponse(additional_context="some context")
        rendered = render_hook_response(
            response, client="opencode", event_name="PreToolUse"
        )
        assert rendered == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "some context",
            }
        }

    def test_opencode_deny_is_adapter_internal_json(self) -> None:
        response = HookResponse(permission_decision="deny", reason="blocked")
        rendered = render_hook_response(
            response, client="opencode", event_name="PreToolUse"
        )
        assert rendered == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "blocked",
            }
        }


# --- Phase 1: Factory alias tests ---


class TestFactoryRendering:
    def test_factory_session_start_same_as_droid(self) -> None:
        response = HookResponse(additional_context="context")
        rendered_droid = render_hook_response(
            response, client="droid", event_name="SessionStart"
        )
        rendered_factory = render_hook_response(
            response, client="factory", event_name="SessionStart"
        )
        assert rendered_droid == rendered_factory


# --- Phase 1: Runner dispatch tests ---


class TestRunnerDispatchNewEvents:
    def test_user_prompt_submit_dispatch(self, monkeypatch: Any, capsys: Any) -> None:
        import io
        from code_briefcase.hooks.runner import run_hook_from_stdin

        # Clean prompt, should noop
        payload = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Hello world",
                "cwd": "/tmp",
            }
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        exit_code = run_hook_from_stdin("user-prompt-submit", client="codex")
        assert exit_code == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {}

    def test_permission_request_dispatch_blocks_destructive(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        import io
        from code_briefcase.hooks.runner import run_hook_from_stdin

        payload = json.dumps(
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "cwd": "/tmp",
            }
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        exit_code = run_hook_from_stdin("permission-request", client="codex")
        assert exit_code == 0  # JSON-control client, exit 0

    def test_stop_dispatch_noop(self, monkeypatch: Any, capsys: Any) -> None:
        import io
        from code_briefcase.hooks.runner import run_hook_from_stdin

        payload = json.dumps({"hook_event_name": "Stop", "cwd": "/tmp"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        exit_code = run_hook_from_stdin("stop", client="codex")
        assert exit_code == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {}

    def test_session_end_dispatch_noop(self, monkeypatch: Any, capsys: Any) -> None:
        import io
        from code_briefcase.hooks.runner import run_hook_from_stdin

        payload = json.dumps({"hook_event_name": "SessionEnd", "cwd": "/tmp"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        exit_code = run_hook_from_stdin("session-end", client="droid")
        assert exit_code == 0

    def test_notification_dispatch_noop(self, monkeypatch: Any, capsys: Any) -> None:
        import io
        from code_briefcase.hooks.runner import run_hook_from_stdin

        payload = json.dumps({"hook_event_name": "Notification", "cwd": "/tmp"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        exit_code = run_hook_from_stdin("notification", client="droid")
        assert exit_code == 0

    def test_pre_compact_dispatch_adds_context(
        self, monkeypatch: Any, capsys: Any, tmp_path: Any
    ) -> None:
        import io
        from code_briefcase.hooks.runner import run_hook_from_stdin

        (tmp_path / "app.py").write_text("def main():\n    return 1\n")
        payload = json.dumps({"hook_event_name": "PreCompact", "cwd": str(tmp_path)})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        exit_code = run_hook_from_stdin("pre-compact", client="droid")
        assert exit_code == 0
        captured = capsys.readouterr()
        rendered = json.loads(captured.out)
        assert rendered["hookSpecificOutput"]["hookEventName"] == "PreCompact"
        assert (
            "Code Briefcase compact context"
            in rendered["hookSpecificOutput"]["additionalContext"]
        )


# --- Phase 1: Event alias tests ---


class TestEventAliases:
    def test_user_prompt_submit_alias(self) -> None:
        from code_briefcase.hooks.runtime import canonical_event_name

        assert canonical_event_name("user-prompt-submit") == "UserPromptSubmit"
        assert canonical_event_name("UserPromptSubmit") == "UserPromptSubmit"

    def test_permission_request_alias(self) -> None:
        from code_briefcase.hooks.runtime import canonical_event_name

        assert canonical_event_name("permission-request") == "PermissionRequest"

    def test_pre_tool_alias(self) -> None:
        from code_briefcase.hooks.runtime import canonical_event_name

        assert canonical_event_name("pre-tool") == "PreToolUse"

    def test_post_tool_alias(self) -> None:
        from code_briefcase.hooks.runtime import canonical_event_name

        assert canonical_event_name("post-tool") == "PostToolUse"

    def test_session_end_alias(self) -> None:
        from code_briefcase.hooks.runtime import canonical_event_name

        assert canonical_event_name("session-end") == "SessionEnd"
        assert canonical_event_name("SessionEnd") == "SessionEnd"

    def test_notification_alias(self) -> None:
        from code_briefcase.hooks.runtime import canonical_event_name

        assert canonical_event_name("notification") == "Notification"

    def test_subagent_start_alias(self) -> None:
        from code_briefcase.hooks.runtime import canonical_event_name

        assert canonical_event_name("subagent-start") == "SubagentStart"

    def test_subagent_stop_alias(self) -> None:
        from code_briefcase.hooks.runtime import canonical_event_name

        assert canonical_event_name("subagent-stop") == "SubagentStop"

    def test_pre_compact_alias(self) -> None:
        from code_briefcase.hooks.runtime import canonical_event_name

        assert canonical_event_name("pre-compact") == "PreCompact"
        assert canonical_event_name("PreCompact") == "PreCompact"


# --- Phase 1: HookResponse extended fields ---


class TestHookResponseExtendedFields:
    def test_decision_block(self) -> None:
        response = HookResponse(decision="block", reason="security risk")
        assert not response.is_noop()
        assert response.decision == "block"
        assert response.reason == "security risk"

    def test_exit_code_metadata(self) -> None:
        response = HookResponse(exit_code=2)
        assert response.exit_code == 2

    def test_noop_ignores_decision_none(self) -> None:
        response = HookResponse(decision=None, reason=None)
        assert response.is_noop()
