from pathlib import Path

from tldr.hooks.post_edit import build_post_edit_response, extract_edited_files
from tldr.hooks.runtime import parse_hook_event


def _event(tmp_path, payload):
    payload = {"event": "postToolUse", "toolName": "Edit", "cwd": str(tmp_path), **payload}
    return parse_hook_event(payload, client="codex")


def test_clean_diagnostics_noop(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    monkeypatch.setattr(
        "tldr.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {"diagnostics": [], "error_count": 0, "warning_count": 0},
    )
    monkeypatch.setattr("tldr.hooks.post_edit.notify_daemon", lambda *a, **k: None)

    assert build_post_edit_response(_event(tmp_path, {"toolInput": {"file_path": "app.py"}})).is_noop()


def test_diagnostics_count_reports_error_and_warning_totals(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")

    def fake(path, language=None):
        name = Path(path).name
        if name == "a.py":
            return {
                "diagnostics": [
                    {"file": "a.py", "line": 1, "column": 1, "source": "pyright", "message": "a bad"}
                ],
                "error_count": 2,
                "warning_count": 1,
            }
        return {
            "diagnostics": [
                {"file": "b.py", "line": 1, "column": 1, "source": "pyright", "message": "b bad"}
            ],
            "error_count": 1,
            "warning_count": 0,
        }

    monkeypatch.setattr("tldr.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr("tldr.hooks.post_edit.notify_daemon", lambda *a, **k: None)

    response = build_post_edit_response(
        _event(
            tmp_path,
            {
                "toolName": "apply_patch",
                "toolInput": {
                    "command": (
                        "*** Begin Patch\n"
                        "*** Update File: a.py\n"
                        "@@\n"
                        " def a():\n"
                        "*** Update File: b.py\n"
                        "@@\n"
                        " def b():\n"
                        "*** End Patch"
                    )
                },
            },
        )
    )

    assert response.diagnostics_count == 4


def test_error_diagnostics_message_includes_count_and_first_diagnostic(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    monkeypatch.setattr(
        "tldr.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {
            "diagnostics": [
                {"file": "app.py", "line": 1, "column": 1, "source": "pyright", "message": "bad"}
            ],
            "error_count": 1,
            "warning_count": 0,
        },
    )
    monkeypatch.setattr("tldr.hooks.post_edit.notify_daemon", lambda *a, **k: None)

    response = build_post_edit_response(_event(tmp_path, {"toolInput": {"file_path": "app.py"}}))

    assert "1 errors, 0 warnings" in response.message
    assert "bad" in response.message
    assert response.diagnostics_count == 1


def test_notify_fallback_marks_dirty_when_daemon_unavailable(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")

    def fail(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("tldr.daemon.query_daemon", fail)
    monkeypatch.setattr(
        "tldr.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {"diagnostics": [], "error_count": 0, "warning_count": 0},
    )

    build_post_edit_response(_event(tmp_path, {"toolInput": {"file_path": "app.py"}}))

    assert (tmp_path / ".tldr" / "cache" / "dirty.json").exists()


def test_unsupported_extension_noop(tmp_path):
    (tmp_path / "README.md").write_text("# hello\n")

    assert build_post_edit_response(_event(tmp_path, {"toolInput": {"file_path": "README.md"}})).is_noop()


def test_codex_tool_response_filepath_finds_file(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    monkeypatch.setattr(
        "tldr.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {"diagnostics": [], "error_count": 0, "warning_count": 0},
    )
    monkeypatch.setattr("tldr.hooks.post_edit.notify_daemon", lambda *a, **k: None)

    assert build_post_edit_response(_event(tmp_path, {"tool_response": {"filePath": "app.py"}})).is_noop()


def test_codex_toolresponse_filepath_finds_file(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    seen = {}

    def fake(path, language=None):
        seen["path"] = Path(path).name
        return {"diagnostics": [], "error_count": 0, "warning_count": 0}

    monkeypatch.setattr("tldr.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr("tldr.hooks.post_edit.notify_daemon", lambda *a, **k: None)

    build_post_edit_response(_event(tmp_path, {"toolResponse": {"filePath": "app.py"}}))

    assert seen["path"] == "app.py"


def test_codex_apply_patch_command_finds_updated_file(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    seen = {}

    def fake(path, language=None):
        seen["path"] = Path(path).name
        return {
            "diagnostics": [
                {"file": "app.py", "line": 1, "column": 1, "source": "pyright", "message": "bad"}
            ],
            "error_count": 1,
            "warning_count": 0,
        }

    monkeypatch.setattr("tldr.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr("tldr.hooks.post_edit.notify_daemon", lambda *a, **k: None)

    response = build_post_edit_response(
        _event(
            tmp_path,
            {
                "toolName": "apply_patch",
                "toolInput": {
                    "command": "*** Begin Patch\n*** Update File: app.py\n@@\n def main():\n*** End Patch"
                },
            },
        )
    )

    assert seen["path"] == "app.py"
    assert "bad" in response.additional_context


def test_codex_apply_patch_move_prefers_destination_file(tmp_path, monkeypatch):
    source = tmp_path / "new.py"
    source.write_text("def main():\n    return 1\n")
    seen = {}

    def fake(path, language=None):
        seen["path"] = Path(path).name
        return {
            "diagnostics": [
                {"file": "new.py", "line": 1, "column": 1, "source": "pyright", "message": "bad"}
            ],
            "error_count": 1,
            "warning_count": 0,
        }

    monkeypatch.setattr("tldr.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr("tldr.hooks.post_edit.notify_daemon", lambda *a, **k: None)

    response = build_post_edit_response(
        _event(
            tmp_path,
            {
                "toolName": "apply_patch",
                "toolInput": {
                    "command": (
                        "*** Begin Patch\n"
                        "*** Update File: old.py\n"
                        "*** Move to: new.py\n"
                        "@@\n"
                        " def main():\n"
                        "*** End Patch"
                    )
                },
            },
        )
    )

    assert seen["path"] == "new.py"
    assert "bad" in response.additional_context


def test_codex_apply_patch_checks_all_updated_files(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")
    seen = []

    def fake(path, language=None):
        name = Path(path).name
        seen.append(name)
        if name == "b.py":
            return {
                "diagnostics": [
                    {"file": "b.py", "line": 1, "column": 1, "source": "pyright", "message": "b bad"}
                ],
                "error_count": 1,
                "warning_count": 0,
            }
        return {"diagnostics": [], "error_count": 0, "warning_count": 0}

    monkeypatch.setattr("tldr.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr("tldr.hooks.post_edit.notify_daemon", lambda *a, **k: None)

    response = build_post_edit_response(
        _event(
            tmp_path,
            {
                "toolName": "apply_patch",
                "toolInput": {
                    "command": (
                        "*** Begin Patch\n"
                        "*** Update File: a.py\n"
                        "@@\n"
                        " def a():\n"
                        "*** Update File: b.py\n"
                        "@@\n"
                        " def b():\n"
                        "*** End Patch"
                    )
                },
            },
        )
    )

    assert seen == ["a.py", "b.py"]
    assert "b bad" in response.additional_context


def test_codex_apply_patch_combines_diagnostics_from_multiple_files(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")

    def fake(path, language=None):
        name = Path(path).name
        return {
            "diagnostics": [
                {"file": name, "line": 1, "column": 1, "source": "pyright", "message": f"{name} bad"}
            ],
            "error_count": 1,
            "warning_count": 0,
        }

    monkeypatch.setattr("tldr.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr("tldr.hooks.post_edit.notify_daemon", lambda *a, **k: None)

    response = build_post_edit_response(
        _event(
            tmp_path,
            {
                "toolName": "apply_patch",
                "toolInput": {
                    "command": (
                        "*** Begin Patch\n"
                        "*** Update File: a.py\n"
                        "@@\n"
                        " def a():\n"
                        "*** Update File: b.py\n"
                        "@@\n"
                        " def b():\n"
                        "*** End Patch"
                    )
                },
            },
        )
    )

    assert "a.py bad" in response.additional_context
    assert "b.py bad" in response.additional_context
    assert "\n\nTLDR diagnostics for b.py" in response.additional_context


def test_codex_apply_patch_keeps_missing_paths_when_other_paths_exist(tmp_path):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")

    event = _event(
        tmp_path,
        {
            "toolName": "apply_patch",
            "toolInput": {
                "command": (
                    "*** Begin Patch\n"
                    "*** Update File: a.py\n"
                    "@@\n"
                    " def a():\n"
                    "*** Add File: b.py\n"
                    "+def b():\n"
                    "+    return 2\n"
                    "*** End Patch"
                )
            },
        },
    )

    assert [path.name for path in extract_edited_files(event)] == ["a.py", "b.py"]
