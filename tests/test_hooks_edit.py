from typing import Any

from code_briefcase.hooks.edit import build_pre_edit_response
from code_briefcase.hooks.read import build_read_response
from code_briefcase.hooks.runtime import parse_hook_event


def _event(
    tmp_path: Any,
    tool_name: Any,
    file_name: Any,
    *,
    session_id: str | None = None,
) -> Any:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": file_name},
        "cwd": str(tmp_path),
    }
    if session_id:
        payload["session_id"] = session_id
    return parse_hook_event(payload, client="claude")


def test_edit_event_on_code_file_returns_structure(tmp_path: Any) -> None:
    (tmp_path / "auth.py").write_text(
        "import os\n\n"
        "class AuthError(Exception):\n"
        "    pass\n\n"
        "def login(username: str, password: str) -> bool:\n"
        "    return True\n"
    )

    response = build_pre_edit_response(_event(tmp_path, "Edit", "auth.py"))

    assert "login" in (response.additional_context or "")
    assert "AuthError" in (response.additional_context or "")
    assert "[Code Briefcase pre-edit context:" in (response.additional_context or "")


def test_write_new_file_noops_without_crashing(tmp_path: Any) -> None:
    assert build_pre_edit_response(_event(tmp_path, "Write", "new.py")).is_noop()


def test_markdown_edit_is_unsupported(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("# hello\n")

    result = build_pre_edit_response(_event(tmp_path, "Edit", "README.md"))

    assert result.status == "skipped"
    assert result.noop_reason == "markdown_unsupported"


def test_output_stays_under_budget(tmp_path: Any) -> None:
    (tmp_path / "big.py").write_text(
        "\n".join(f"def f{i}():\n    return {i}" for i in range(200))
    )

    response = build_pre_edit_response(_event(tmp_path, "Edit", "big.py"), budget=100)

    assert len(response.additional_context or "") <= 700


def test_codex_apply_patch_is_suppressed(tmp_path: Any) -> None:
    source = tmp_path / "auth.py"
    source.write_text("def login():\n    return True\n")
    event = parse_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: auth.py\n@@\n def login():\n*** End Patch"
            },
            "cwd": str(tmp_path),
        },
        client="codex",
    )

    result = build_pre_edit_response(event)

    assert result.status == "skipped"
    assert result.noop_reason == "apply_patch_pre_edit_suppressed"
    assert result.additional_context is None


def test_external_path_skips_without_crashing(tmp_path: Any) -> None:
    external = tmp_path.parent / "external_edit.py"

    response = build_pre_edit_response(_event(tmp_path, "Write", str(external)))

    assert response.status == "skipped"
    assert response.trigger_files == []


def test_existing_external_path_skips_without_context(tmp_path: Any) -> None:
    external = tmp_path.parent / "external_existing_edit.py"
    external.write_text("def main():\n    return 1\n", encoding="utf-8")

    response = build_pre_edit_response(_event(tmp_path, "Edit", str(external)))

    assert response.status == "skipped"
    assert response.additional_context is None
    assert response.trigger_files == []


def test_likely_symbol_uses_pending_framing(tmp_path: Any) -> None:
    (tmp_path / "svc.py").write_text(
        "class Service:\n    pass\n\ndef handle():\n    return True\n"
    )
    event = parse_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "svc.py",
                "old_string": "def handle():",
                "new_string": "def handle_request():",
            },
            "cwd": str(tmp_path),
        },
        client="claude",
    )

    response = build_pre_edit_response(event)

    assert "Your pending edit introduces or modifies: handle_request" in (
        response.additional_context or ""
    )
    assert "will appear in the file structure above after this edit applies" not in (
        response.additional_context or ""
    )


def test_likely_symbol_does_not_claim_deleted_symbol_will_reappear(
    tmp_path: Any,
) -> None:
    (tmp_path / "svc.py").write_text("def handle():\n    return True\n")
    event = parse_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "svc.py",
                "old_string": "def handle():\n    return True\n",
                "new_string": "",
            },
            "cwd": str(tmp_path),
        },
        client="claude",
    )

    response = build_pre_edit_response(event)

    assert "Your pending edit introduces or modifies:" not in (
        response.additional_context or ""
    )
    assert "will appear in the file structure above after this edit applies" not in (
        response.additional_context or ""
    )


def test_pre_edit_after_pre_read_same_session_is_throttled(tmp_path: Any) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        "import os\n\n"
        "def helper(value: int) -> int:\n"
        "    return value + 1\n\n"
        "def main() -> int:\n"
        "    return helper(1)\n" + "\n".join(f"VALUE_{i} = {i}" for i in range(300))
    )

    read_event = parse_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "app.py"},
            "cwd": str(tmp_path),
            "session_id": "s1",
        },
        client="claude",
    )
    read_result = build_read_response(read_event)
    edit_result = build_pre_edit_response(
        _event(tmp_path, "Edit", "app.py", session_id="s1")
    )

    assert read_result.status == "ok"
    assert edit_result.status == "skipped"
    assert edit_result.noop_reason == "read_nav_map_recently_surfaced"


def test_pre_edit_resurfaces_after_file_mtime_changes(tmp_path: Any) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        "def helper(value: int) -> int:\n"
        "    return value + 1\n" + "\n".join(f"VALUE_{i} = {i}" for i in range(300))
    )

    read_event = parse_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "app.py"},
            "cwd": str(tmp_path),
            "session_id": "s1",
        },
        client="claude",
    )
    assert build_read_response(read_event).status == "ok"
    assert (
        build_pre_edit_response(
            _event(tmp_path, "Edit", "app.py", session_id="s1")
        ).noop_reason
        == "read_nav_map_recently_surfaced"
    )

    source.write_text(
        source.read_text(encoding="utf-8") + "\ndef added() -> int:\n    return 0\n",
        encoding="utf-8",
    )
    edit_after_touch = build_pre_edit_response(
        _event(tmp_path, "Edit", "app.py", session_id="s1")
    )
    assert edit_after_touch.status == "ok"
    assert "added" in (edit_after_touch.additional_context or "")
