from typing import Any
from code_briefcase.hooks.edit import build_pre_edit_response
from code_briefcase.hooks.runtime import parse_hook_event


def _event(tmp_path: Any, tool_name: Any, file_name: Any) -> Any:
    return parse_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": {"file_path": file_name},
            "cwd": str(tmp_path),
        },
        client="claude",
    )


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
    assert "BEFORE your pending edit lands" in (response.additional_context or "")


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
