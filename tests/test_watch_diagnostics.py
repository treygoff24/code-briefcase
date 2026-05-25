from __future__ import annotations

from pathlib import Path

from code_briefcase.daemon.protocol import DaemonResponseKind
from code_briefcase.daemon.startup import DaemonResponse
from code_briefcase.hooks.post_edit import build_post_edit_response, _watch_diagnostics_enabled
from code_briefcase.hooks.runtime import parse_hook_event


def _event(tmp_path: Path, payload: dict):
    payload = {"event": "postToolUse", "toolName": "Edit", "cwd": str(tmp_path), **payload}
    return parse_hook_event(payload, client="codex")


def _source(tmp_path: Path, name: str = "app.ts") -> Path:
    source = tmp_path / name
    source.write_text("const answer: string = 42;\n", encoding="utf-8")
    return source


def test_watch_diagnostics_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS", raising=False)
    monkeypatch.delenv("TLDR_WATCH_DIAGNOSTICS", raising=False)

    assert _watch_diagnostics_enabled() is False


def test_code_briefcase_watch_diagnostics_overrides_legacy_tldr_env(monkeypatch):
    monkeypatch.setenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS", "0")
    monkeypatch.setenv("TLDR_WATCH_DIAGNOSTICS", "1")
    assert _watch_diagnostics_enabled() is False

    monkeypatch.setenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS", "1")
    monkeypatch.setenv("TLDR_WATCH_DIAGNOSTICS", "0")
    assert _watch_diagnostics_enabled() is True


def test_legacy_tldr_watch_diagnostics_enables_when_new_env_unset(monkeypatch):
    monkeypatch.delenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS", raising=False)
    monkeypatch.setenv("TLDR_WATCH_DIAGNOSTICS", "1")

    assert _watch_diagnostics_enabled() is True


def test_watch_diagnostics_falsey_values_disable(monkeypatch):
    for value in ("0", "false", "no", "off", "disabled", ""):
        monkeypatch.setenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS", value)
        monkeypatch.setenv("TLDR_WATCH_DIAGNOSTICS", "1")
        assert _watch_diagnostics_enabled() is False


def test_post_edit_fresh_watcher_skips_sync_get_diagnostics(tmp_path, monkeypatch):
    _source(tmp_path)
    monkeypatch.setenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS", "1")
    monkeypatch.setattr("code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None)
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sync fallback should not run")),
    )

    def fake_query(*_args, **_kwargs):
        return DaemonResponse(
            DaemonResponseKind.OK,
            payload={
                "status": "ok",
                "watcher_status": "fresh",
                "diagnostics": [
                    {
                        "file": str(tmp_path / "app.ts"),
                        "line": 1,
                        "column": 7,
                        "severity": "error",
                        "source": "tsc-watch",
                        "message": "bad type",
                    }
                ],
                "error_count": 1,
                "warning_count": 0,
                "wait_ms": 12,
                "age_ms": 3,
                "batch_seq": 4,
                "backend": "tsc-watch",
            },
        )

    monkeypatch.setattr("code_briefcase.daemon.query_or_start_daemon", fake_query)

    response = build_post_edit_response(_event(tmp_path, {"toolInput": {"file_path": "app.ts"}}))

    assert response.diagnostics_count == 1
    assert "bad type" in response.additional_context
    assert response.watch_diagnostics_used is True
    assert response.watch_diagnostics_status == "fresh"


def test_post_edit_fallback_status_uses_local_sync_only(tmp_path, monkeypatch):
    _source(tmp_path)
    monkeypatch.setenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS", "1")
    monkeypatch.setattr("code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None)
    seen_commands = []

    def fake_query(_project, command, **_kwargs):
        seen_commands.append(command)
        assert command.get("cmd") != "diagnostics"
        return DaemonResponse(DaemonResponseKind.UNREACHABLE, message="no daemon")

    sync_calls = []

    def fake_sync(path, language=None):
        sync_calls.append((Path(path).name, language))
        return {
            "diagnostics": [
                {
                    "file": str(tmp_path / "app.ts"),
                    "line": 1,
                    "column": 7,
                    "severity": "error",
                    "source": "tsc",
                    "message": "sync bad",
                }
            ],
            "error_count": 1,
            "warning_count": 0,
        }

    monkeypatch.setattr("code_briefcase.daemon.query_or_start_daemon", fake_query)
    monkeypatch.setattr("code_briefcase.hooks.post_edit.get_diagnostics", fake_sync)

    response = build_post_edit_response(_event(tmp_path, {"toolInput": {"file_path": "app.ts"}}))

    assert sync_calls == [("app.ts", "typescript")]
    assert all(command["cmd"] == "watchers" for command in seen_commands)
    assert "sync bad" in response.additional_context
    assert response.watch_diagnostics_status == "fallback_required"
    assert response.watch_diagnostics_fallback_reason == "no daemon"


def test_post_edit_pending_watcher_does_not_sync_fallback(tmp_path, monkeypatch):
    _source(tmp_path)
    monkeypatch.setenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS", "1")
    monkeypatch.setattr("code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None)
    monkeypatch.setattr(
        "code_briefcase.hooks.post_edit.get_diagnostics",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("pending must not block on sync")),
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

    response = build_post_edit_response(_event(tmp_path, {"toolInput": {"file_path": "app.ts"}}))

    assert response.status == "ok"
    assert "fresh results are still pending" in response.additional_context
    assert response.watch_diagnostics_status == "pending"
    assert response.watch_diagnostics_used is True


def test_post_edit_multi_file_combines_watcher_and_sync_fallback(tmp_path, monkeypatch):
    _source(tmp_path, "a.ts")
    _source(tmp_path, "b.ts")
    monkeypatch.setenv("CODE_BRIEFCASE_WATCH_DIAGNOSTICS", "1")
    monkeypatch.setattr("code_briefcase.hooks.post_edit.notify_daemon", lambda *a, **k: None)

    def fake_query(_project, command, **_kwargs):
        if Path(command["file"]).name == "a.ts":
            return DaemonResponse(
                DaemonResponseKind.OK,
                payload={
                    "status": "ok",
                    "watcher_status": "fresh",
                    "diagnostics": [
                        {
                            "file": str(tmp_path / "a.ts"),
                            "line": 1,
                            "column": 7,
                            "severity": "error",
                            "source": "tsc-watch",
                            "message": "a bad",
                        }
                    ],
                    "error_count": 1,
                    "warning_count": 0,
                    "wait_ms": 1,
                },
            )
        return DaemonResponse(
            DaemonResponseKind.OK,
            payload={
                "status": "ok",
                "watcher_status": "unhealthy",
                "diagnostics": [],
                "error_count": 0,
                "warning_count": 0,
                "fallback_reason": "adapter_crashed",
            },
        )

    sync_calls = []

    def fake_sync(path, language=None):
        sync_calls.append(Path(path).name)
        return {
            "diagnostics": [
                {
                    "file": str(tmp_path / "b.ts"),
                    "line": 1,
                    "column": 7,
                    "severity": "warning",
                    "source": "tsc",
                    "message": "b warning",
                }
            ],
            "error_count": 0,
            "warning_count": 1,
        }

    monkeypatch.setattr("code_briefcase.daemon.query_or_start_daemon", fake_query)
    monkeypatch.setattr("code_briefcase.hooks.post_edit.get_diagnostics", fake_sync)

    payload = {
        "toolName": "apply_patch",
        "toolInput": {
            "command": (
                "*** Begin Patch\n"
                "*** Update File: a.ts\n"
                "@@\n"
                " const a = 1;\n"
                "*** Update File: b.ts\n"
                "@@\n"
                " const b = 2;\n"
                "*** End Patch"
            )
        },
    }
    response = build_post_edit_response(_event(tmp_path, payload))

    assert sync_calls == ["b.ts"]
    assert response.diagnostics_count == 2
    assert "a bad" in response.additional_context
    assert "b warning" in response.additional_context
    assert response.watch_diagnostics_statuses == ["fresh", "unhealthy"]
