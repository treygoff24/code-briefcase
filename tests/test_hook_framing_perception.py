from typing import Any
import json

import pytest

from code_briefcase.hooks.edit import EDIT_TOOLS, build_pre_edit_response
from code_briefcase.hooks.post_edit import build_post_edit_response
from code_briefcase.hooks.runtime import parse_hook_event, render_hook_response


def _code_fixture(tmp_path: Any) -> Any:
    path = tmp_path / "auth.py"
    path.write_text(
        "import os\n\n"
        "class AuthError(Exception):\n"
        "    pass\n\n"
        "def login(username: str, password: str) -> bool:\n"
        "    return True\n"
    )
    return path


def _pre_edit_event(
    tmp_path: Any, tool_name: str, tool_input: dict | None = None
) -> Any:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input or {"file_path": "auth.py"},
        "cwd": str(tmp_path),
    }
    return parse_hook_event(payload, client="claude")


def _tool_input_for(tool_name: str) -> dict:
    if tool_name == "Write":
        return {"file_path": "auth.py", "content": "def login():\n    return True\n"}
    if tool_name == "MultiEdit":
        return {
            "file_path": "auth.py",
            "edits": [{"old_string": "return True", "new_string": "return False"}],
        }
    if tool_name == "Update":
        return {
            "file_path": "auth.py",
            "old_string": "return True",
            "new_string": "return False",
        }
    return {
        "file_path": "auth.py",
        "old_string": "return True",
        "new_string": "return False",
    }


class TestPreEditFraming:
    @pytest.mark.parametrize("tool_name", sorted(EDIT_TOOLS - {"apply_patch"}))
    def test_rendered_context_includes_temporal_framing(
        self, tmp_path: Any, tool_name: Any
    ) -> None:
        _code_fixture(tmp_path)
        event = _pre_edit_event(tmp_path, tool_name, _tool_input_for(tool_name))
        execution = build_pre_edit_response(event)
        assert (
            execution.status == "ok"
        ), f"{tool_name} should emit context, got {execution.status}"

        rendered = render_hook_response(
            execution.response,
            client="claude",
            event_name="PreToolUse",
        )
        context = rendered["hookSpecificOutput"]["additionalContext"]
        assert "BEFORE your pending edit lands" in context
        assert "NOT blocked" in context or "Proceed normally" in context


class TestPostEditFraming:
    def test_clean_edit_confirmation_framing(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        source = tmp_path / "app.py"
        source.write_text("def main():\n    return 1\n")
        monkeypatch.setattr(
            "code_briefcase.hooks.post_edit.get_diagnostics",
            lambda *a, **k: {"diagnostics": [], "error_count": 0, "warning_count": 0},
        )
        monkeypatch.setattr(
            "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
        )

        event = parse_hook_event(
            {
                "event": "postToolUse",
                "toolName": "Edit",
                "toolInput": {"file_path": "app.py"},
                "cwd": str(tmp_path),
            },
            client="claude",
        )
        execution = build_post_edit_response(event)
        rendered = render_hook_response(
            execution.response,
            client="claude",
            event_name="PostToolUse",
        )
        context = rendered["hookSpecificOutput"]["additionalContext"]
        assert "no diagnostics were surfaced" in context


class TestNonCodeFileEditFraming:
    """Covers the non-code-file edit branch in `build_file_context_for_path`
    (the `[Code Briefcase pre-edit context — ...]` rewrite around L580).
    """

    def test_yaml_edit_includes_pre_edit_disclaimer(self, tmp_path: Any) -> None:
        (tmp_path / "config.yaml").write_text(
            "name: example\nversion: 1\nfeatures:\n  - alpha\n  - beta\n"
        )
        event = parse_hook_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "config.yaml",
                    "old_string": "- beta",
                    "new_string": "- beta\n  - gamma",
                },
                "cwd": str(tmp_path),
            },
            client="claude",
        )
        execution = build_pre_edit_response(event)
        assert execution.status == "ok"

        context = execution.additional_context or ""
        # Header was rewritten from "[Code Briefcase " → "[Code Briefcase pre-edit context — ..."
        assert "[Code Briefcase pre-edit context —" in context
        # Disclaimer paragraph appended
        assert "Pre-edit snapshot only" in context
        assert "does NOT block or modify" in context


class TestApplyPatchSkip:
    def test_codex_apply_patch_pre_edit_is_suppressed(self, tmp_path: Any) -> None:
        source = tmp_path / "app.py"
        source.write_text("def main():\n    return 1\n")
        event = parse_hook_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "apply_patch",
                "tool_input": {
                    "command": (
                        "*** Begin Patch\n*** Update File: app.py\n@@\n def main():\n*** End Patch"
                    )
                },
                "cwd": str(tmp_path),
            },
            client="codex",
        )

        execution = build_pre_edit_response(event)
        assert execution.status == "skipped"
        assert execution.noop_reason == "apply_patch_pre_edit_suppressed"

        rendered = render_hook_response(
            execution.response,
            client="codex",
            event_name="PreToolUse",
        )
        assert rendered == {}
        assert "additionalContext" not in json.dumps(rendered)
