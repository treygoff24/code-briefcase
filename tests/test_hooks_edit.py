from tldr.hooks.edit import build_pre_edit_response
from tldr.hooks.runtime import parse_hook_event


def _event(tmp_path, tool_name, file_name):
    return parse_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": {"file_path": file_name},
            "cwd": str(tmp_path),
        },
        client="claude",
    )


def test_edit_event_on_code_file_returns_structure(tmp_path):
    (tmp_path / "auth.py").write_text(
        "import os\n\n"
        "class AuthError(Exception):\n"
        "    pass\n\n"
        "def login(username: str, password: str) -> bool:\n"
        "    return True\n"
    )

    response = build_pre_edit_response(_event(tmp_path, "Edit", "auth.py"))

    assert "login" in response.additional_context
    assert "AuthError" in response.additional_context


def test_write_new_file_noops_without_crashing(tmp_path):
    assert build_pre_edit_response(_event(tmp_path, "Write", "new.py")).is_noop()


def test_non_code_file_noops(tmp_path):
    (tmp_path / "README.md").write_text("# hello\n")

    assert build_pre_edit_response(_event(tmp_path, "Edit", "README.md")).is_noop()


def test_output_stays_under_budget(tmp_path):
    (tmp_path / "big.py").write_text("\n".join(f"def f{i}():\n    return {i}" for i in range(200)))

    response = build_pre_edit_response(_event(tmp_path, "Edit", "big.py"), budget=100)

    assert len(response.additional_context) <= 420


def test_codex_apply_patch_update_returns_existing_file_context(tmp_path):
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

    response = build_pre_edit_response(event)

    assert "login" in response.additional_context


def test_external_path_skips_without_crashing(tmp_path):
    external = tmp_path.parent / "external_edit.py"

    response = build_pre_edit_response(_event(tmp_path, "Write", str(external)))

    assert response.status == "skipped"
