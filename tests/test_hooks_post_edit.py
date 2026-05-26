from typing import Any
import logging
from pathlib import Path

import pytest

from code_briefcase.daemon.protocol import DaemonResponseKind
from code_briefcase.daemon.startup import DaemonResponse
from code_briefcase.hooks.post_edit import (
    WATCH_ENV,
    _watch_diagnostics_enabled,
    build_post_edit_response,
    extract_edited_files,
)
from code_briefcase.hooks.runtime import parse_hook_event


def _event(tmp_path: Any, payload: Any) -> Any:
    payload = {
        "event": "postToolUse",
        "toolName": "Edit",
        "cwd": str(tmp_path),
        **payload,
    }
    return parse_hook_event(payload, client="codex")


def test_post_edit_skips_excluded_vendor_paths(tmp_path: Any, monkeypatch: Any) -> None:
    vendor = tmp_path / "node_modules" / "pkg" / "index.js"
    vendor.parent.mkdir(parents=True)
    vendor.write_text("export const x = 1;\n", encoding="utf-8")
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

    response = build_post_edit_response(
        _event(
            tmp_path,
            {"toolInput": {"file_path": str(vendor.relative_to(tmp_path))}},
        )
    )

    assert response.status == "skipped"
    assert response.noop_reason == "no_edit_targets"


def test_clean_diagnostics_emits_confirmation(tmp_path: Any, monkeypatch: Any) -> None:
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {"diagnostics": [], "error_count": 0, "warning_count": 0},
    )
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

    response = build_post_edit_response(
        _event(tmp_path, {"toolInput": {"file_path": "app.py"}})
    )
    assert response.status == "noop"
    assert response.noop_reason == "clean_no_diagnostics"
    assert response.additional_context is None


def test_clean_diagnostics_verbose_confirmation(
    tmp_path: Any, monkeypatch: Any
) -> None:
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    monkeypatch.setenv("CODE_BRIEFCASE_POST_EDIT_VERBOSE", "1")
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {"diagnostics": [], "error_count": 0, "warning_count": 0},
    )
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

    response = build_post_edit_response(
        _event(tmp_path, {"toolInput": {"file_path": "app.py"}})
    )
    assert response.status == "ok"
    assert response.noop_reason == "clean_no_diagnostics"
    assert "no diagnostics were surfaced" in (response.additional_context or "")
    assert "app.py" in (response.additional_context or "")


def test_diagnostics_count_reports_error_and_warning_totals(
    tmp_path: Any, monkeypatch: Any
) -> None:
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")

    def fake(path: Any, language: Any = None) -> Any:
        name = Path(path).name
        if name == "a.py":
            return {
                "diagnostics": [
                    {
                        "file": "a.py",
                        "line": 1,
                        "column": 1,
                        "source": "pyright",
                        "message": "a bad",
                    }
                ],
                "error_count": 2,
                "warning_count": 1,
            }
        return {
            "diagnostics": [
                {
                    "file": "b.py",
                    "line": 1,
                    "column": 1,
                    "source": "pyright",
                    "message": "b bad",
                }
            ],
            "error_count": 1,
            "warning_count": 0,
        }

    monkeypatch.setattr("code_briefcase.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

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


def test_error_diagnostics_message_includes_count_and_first_diagnostic(
    tmp_path: Any, monkeypatch: Any
) -> None:
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {
            "diagnostics": [
                {
                    "file": "app.py",
                    "line": 1,
                    "column": 1,
                    "source": "pyright",
                    "message": "bad",
                }
            ],
            "error_count": 1,
            "warning_count": 0,
        },
    )
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

    response = build_post_edit_response(
        _event(tmp_path, {"toolInput": {"file_path": "app.py"}})
    )

    assert "1 errors, 0 warnings" in (response.message or "")
    assert "bad" in (response.message or "")
    assert response.diagnostics_count == 1


def test_notify_fallback_marks_dirty_when_daemon_unavailable(
    tmp_path: Any, monkeypatch: Any
) -> None:
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError

    monkeypatch.setattr("code_briefcase.daemon.query_daemon", fail)
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {"diagnostics": [], "error_count": 0, "warning_count": 0},
    )

    build_post_edit_response(_event(tmp_path, {"toolInput": {"file_path": "app.py"}}))

    assert (tmp_path / ".code-briefcase" / "cache" / "dirty.json").exists()


def test_markdown_post_edit_is_unsupported(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("# hello\n")

    response = build_post_edit_response(
        _event(tmp_path, {"toolInput": {"file_path": "README.md"}})
    )

    assert response.status == "skipped"
    assert response.noop_reason == "markdown_unsupported"


def test_test_file_post_edit_is_eligible(tmp_path: Any, monkeypatch: Any) -> None:
    source = tmp_path / "tests" / "test_app.py"
    source.parent.mkdir(parents=True)
    source.write_text("def test_main():\n    assert True\n")
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {"diagnostics": [], "error_count": 0, "warning_count": 0},
    )
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

    response = build_post_edit_response(
        _event(tmp_path, {"toolInput": {"file_path": "tests/test_app.py"}})
    )

    assert response.status == "noop"
    assert response.noop_reason == "clean_no_diagnostics"


def test_external_path_skips_without_crashing(tmp_path: Any, monkeypatch: Any) -> None:
    external = tmp_path.parent / "external_post_edit.py"
    external.write_text("def main():\n    return 1\n")
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {"diagnostics": [], "error_count": 0, "warning_count": 0},
    )
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

    response = build_post_edit_response(
        _event(tmp_path, {"toolInput": {"file_path": str(external)}})
    )

    assert response.status == "skipped"


def test_codex_tool_response_filepath_finds_file(
    tmp_path: Any, monkeypatch: Any
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

    response = build_post_edit_response(
        _event(tmp_path, {"tool_response": {"filePath": "app.py"}})
    )
    assert response.noop_reason == "clean_no_diagnostics"
    assert response.status == "noop"


def test_codex_toolresponse_filepath_finds_file(
    tmp_path: Any, monkeypatch: Any
) -> None:
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    seen = {}

    def fake(path: Any, language: Any = None) -> Any:
        seen["path"] = Path(path).name
        return {"diagnostics": [], "error_count": 0, "warning_count": 0}

    monkeypatch.setattr("code_briefcase.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

    build_post_edit_response(_event(tmp_path, {"toolResponse": {"filePath": "app.py"}}))

    assert seen["path"] == "app.py"


def test_codex_apply_patch_command_finds_updated_file(
    tmp_path: Any, monkeypatch: Any
) -> None:
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")
    seen = {}

    def fake(path: Any, language: Any = None) -> Any:
        seen["path"] = Path(path).name
        return {
            "diagnostics": [
                {
                    "file": "app.py",
                    "line": 1,
                    "column": 1,
                    "source": "pyright",
                    "message": "bad",
                }
            ],
            "error_count": 1,
            "warning_count": 0,
        }

    monkeypatch.setattr("code_briefcase.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

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
    assert "bad" in (response.additional_context or "")


def test_codex_apply_patch_move_prefers_destination_file(
    tmp_path: Any, monkeypatch: Any
) -> None:
    source = tmp_path / "new.py"
    source.write_text("def main():\n    return 1\n")
    seen = {}

    def fake(path: Any, language: Any = None) -> Any:
        seen["path"] = Path(path).name
        return {
            "diagnostics": [
                {
                    "file": "new.py",
                    "line": 1,
                    "column": 1,
                    "source": "pyright",
                    "message": "bad",
                }
            ],
            "error_count": 1,
            "warning_count": 0,
        }

    monkeypatch.setattr("code_briefcase.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

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
    assert "bad" in (response.additional_context or "")


def test_codex_apply_patch_checks_all_updated_files(
    tmp_path: Any, monkeypatch: Any
) -> None:
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")
    seen = []

    def fake(path: Any, language: Any = None) -> Any:
        name = Path(path).name
        seen.append(name)
        if name == "b.py":
            return {
                "diagnostics": [
                    {
                        "file": "b.py",
                        "line": 1,
                        "column": 1,
                        "source": "pyright",
                        "message": "b bad",
                    }
                ],
                "error_count": 1,
                "warning_count": 0,
            }
        return {"diagnostics": [], "error_count": 0, "warning_count": 0}

    monkeypatch.setattr("code_briefcase.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

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
    assert "b bad" in (response.additional_context or "")


def test_codex_apply_patch_combines_diagnostics_from_multiple_files(
    tmp_path: Any, monkeypatch: Any
) -> None:
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")

    def fake(path: Any, language: Any = None) -> Any:
        name = Path(path).name
        return {
            "diagnostics": [
                {
                    "file": name,
                    "line": 1,
                    "column": 1,
                    "source": "pyright",
                    "message": f"{name} bad",
                }
            ],
            "error_count": 1,
            "warning_count": 0,
        }

    monkeypatch.setattr("code_briefcase.hooks.post_edit.get_diagnostics", fake)
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )

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

    assert "a.py bad" in (response.additional_context or "")
    assert "b.py bad" in (response.additional_context or "")
    assert "\n\nCode Briefcase diagnostics for b.py" in (
        response.additional_context or ""
    )


def test_codex_apply_patch_keeps_missing_paths_when_other_paths_exist(
    tmp_path: Any,
) -> None:
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


def test_clean_confirmation_is_silent_by_default(
    tmp_path: Any, monkeypatch: Any
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
    monkeypatch.delenv("CODE_BRIEFCASE_POST_EDIT_VERBOSE", raising=False)
    monkeypatch.delenv("CODE_BRIEFCASE_POST_EDIT_CLEAN_CONFIRM", raising=False)

    response = build_post_edit_response(
        _event(tmp_path, {"toolInput": {"file_path": "app.py"}})
    )

    assert response.status == "noop"
    assert response.noop_reason == "clean_no_diagnostics"
    assert response.additional_context is None


def test_clean_confirmation_lists_multiple_files(
    tmp_path: Any, monkeypatch: Any
) -> None:
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: {"diagnostics": [], "error_count": 0, "warning_count": 0},
    )
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )
    monkeypatch.setenv("CODE_BRIEFCASE_POST_EDIT_VERBOSE", "1")

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

    assert response.status == "ok"
    assert response.noop_reason == "clean_no_diagnostics"
    assert "a.py" in (response.additional_context or "")
    assert "b.py" in (response.additional_context or "")


@pytest.mark.parametrize(
    "value",
    ("1", "true", "yes", "on", "enabled", "enable", "y", "t", " Yes ", "ENABLED"),
)
def test_watch_diagnostics_truthy_values_enable(monkeypatch: Any, value: Any) -> None:
    monkeypatch.setenv(WATCH_ENV, value)
    monkeypatch.delenv("TLDR_WATCH_DIAGNOSTICS", raising=False)
    assert _watch_diagnostics_enabled() is True


@pytest.mark.parametrize(
    "value",
    ("0", "false", "no", "off", "disabled", "disable", "n", "f", "", " FALSE "),
)
def test_watch_diagnostics_falsey_values_disable(monkeypatch: Any, value: Any) -> None:
    monkeypatch.setenv(WATCH_ENV, value)
    monkeypatch.setenv("TLDR_WATCH_DIAGNOSTICS", "1")
    assert _watch_diagnostics_enabled() is False


def test_watch_diagnostics_unrecognized_value_warns_and_disables(
    monkeypatch: Any, caplog: Any
) -> None:
    monkeypatch.setenv(WATCH_ENV, "maybe")
    monkeypatch.delenv("TLDR_WATCH_DIAGNOSTICS", raising=False)

    with caplog.at_level(logging.WARNING):
        assert _watch_diagnostics_enabled() is False

    assert len(caplog.records) == 1
    assert "Unrecognized" in caplog.records[0].message
    assert "maybe" in caplog.records[0].message
    assert WATCH_ENV in caplog.records[0].message


def test_post_edit_pending_watcher_surfaces_notice_not_diagnostic(
    tmp_path: Any, monkeypatch: Any
) -> None:
    source = tmp_path / "app.ts"
    source.write_text("const answer: string = 42;\n", encoding="utf-8")
    monkeypatch.setenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS", "1")
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("pending must not block on sync")
        ),
    )
    monkeypatch.setattr(
        "code_briefcase.daemon.query_or_start_daemon",
        lambda *_args, **_kwargs: DaemonResponse(
            DaemonResponseKind.OK,
            payload={
                "status": "ok",
                "watcher_status": "pending",
                "diagnostics": [],
                "error_count": 0,
                "warning_count": 0,
                "wait_ms": 150,
                "backend": "tsc-watch",
            },
        ),
    )

    response = build_post_edit_response(
        _event(tmp_path, {"toolInput": {"file_path": "app.ts"}})
    )

    assert response.diagnostics_count == 0
    assert response.watch_diagnostics_status == "pending"
    assert "[Code Briefcase watcher]" in (response.additional_context or "")
    assert "fresh results are still pending" in (response.additional_context or "")
    assert "Code Briefcase diagnostics for" not in (response.additional_context or "")
