from __future__ import annotations

from code_briefcase.hooks.runtime import parse_hook_event
from code_briefcase.hooks.tool import build_pre_tool_response, extract_shell_file_candidates


def make_event(tmp_path, tool_name: str, tool_input: dict):
    return parse_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": str(tmp_path),
        },
        client="codex",
    )


# --- Shell file-context fan-out is disabled (see tool.py:build_pre_tool_response).
#
# The tests below assert the hook is a no-op for shell commands that previously
# triggered the per-file edit-style symbol dump. Re-enabling the fan-out without
# updating these tests should be the visible signal that the redesign has
# landed — see docs/plans/2026-05-24-pre-tool-shell-context-redesign.md.


def test_pre_tool_is_noop_for_read_like_shell_command(tmp_path):
    path = tmp_path / "src" / "app.ts"
    path.parent.mkdir(parents=True)
    path.write_text("export function main() { return 1 }\n", encoding="utf-8")
    event = make_event(tmp_path, "Bash", {"command": "sed -n '1,80p' src/app.ts"})

    result = build_pre_tool_response(event)

    assert result.status == "noop"
    assert result.noop_reason == "shell_file_context_disabled"


def test_pre_tool_is_noop_for_exec_command(tmp_path):
    path = tmp_path / "src" / "app.ts"
    path.parent.mkdir(parents=True)
    path.write_text("export function main() { return 1 }\n", encoding="utf-8")
    event = make_event(tmp_path, "exec_command", {"command": "nl -ba src/app.ts"})

    result = build_pre_tool_response(event)

    assert result.status == "noop"
    assert result.noop_reason == "shell_file_context_disabled"


def test_pre_tool_is_noop_for_multi_path_command(tmp_path):
    app = tmp_path / "src" / "app.ts"
    test_file = tmp_path / "tests" / "test_app.py"
    app.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    app.write_text("export const foo = 1\n", encoding="utf-8")
    test_file.write_text("def test_foo():\n    assert True\n", encoding="utf-8")
    event = make_event(
        tmp_path,
        "Bash",
        {"command": 'rg -n "foo" src/app.ts tests/test_app.py'},
    )

    result = build_pre_tool_response(event)

    assert result.status == "noop"


def test_pre_tool_is_noop_for_git_diff(tmp_path):
    path = tmp_path / "src" / "app.ts"
    path.parent.mkdir(parents=True)
    path.write_text("export function main() { return 1 }\n", encoding="utf-8")
    event = make_event(tmp_path, "Bash", {"command": "git diff -- src/app.ts"})

    result = build_pre_tool_response(event)

    assert result.status == "noop"


def test_glob_tokens_are_not_expanded(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    event = make_event(tmp_path, "Bash", {"command": "rg foo *.py"})

    candidates = extract_shell_file_candidates(event, event.tool_input["command"])

    assert candidates == []


def test_destructive_command_still_denied(tmp_path):
    event = make_event(tmp_path, "Bash", {"command": "rm -rf /"})

    result = build_pre_tool_response(event)

    assert result.permission_decision == "deny"
