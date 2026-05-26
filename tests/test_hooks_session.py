from typing import Any
from code_briefcase.hooks.runtime import parse_hook_event
from code_briefcase.hooks.session import build_session_start_response


def test_no_crash_on_empty_project(tmp_path: Any, monkeypatch: Any) -> None:
    calls = []
    monkeypatch.setattr(
        "code_briefcase.hooks.session._spawn",
        lambda *args, **kwargs: calls.append(args),
    )

    response = build_session_start_response(parse_hook_event({"cwd": str(tmp_path)}))

    assert not response.is_noop()
    assert calls


def test_no_semantic_index_command(tmp_path: Any, monkeypatch: Any) -> None:
    commands = []
    monkeypatch.setattr(
        "code_briefcase.hooks.session._spawn",
        lambda command, *args, **kwargs: commands.append(command),
    )

    build_session_start_response(parse_hook_event({"cwd": str(tmp_path)}))

    assert all("semantic" not in command for command in commands)


def test_large_repo_skips_warm(tmp_path: Any, monkeypatch: Any) -> None:
    commands = []
    monkeypatch.setattr(
        "code_briefcase.hooks.session._spawn",
        lambda command, *args, **kwargs: commands.append(command),
    )
    monkeypatch.setattr(
        "code_briefcase.hooks.session.count_source_files", lambda *args, **kwargs: 999
    )

    response = build_session_start_response(
        parse_hook_event({"cwd": str(tmp_path)}), max_files=10
    )

    assert "skipped warm" in (response.message or "")
    assert not any("warm" in command for command in commands)


def test_small_repo_schedules_warm(tmp_path: Any, monkeypatch: Any) -> None:
    commands = []
    monkeypatch.setattr(
        "code_briefcase.hooks.session._spawn",
        lambda command, *args, **kwargs: commands.append(command),
    )
    monkeypatch.setattr(
        "code_briefcase.hooks.session.count_source_files", lambda *args, **kwargs: 1
    )

    build_session_start_response(parse_hook_event({"cwd": str(tmp_path)}))

    assert any("warm" in command for command in commands)


def test_background_work_can_be_disabled(tmp_path: Any, monkeypatch: Any) -> None:
    commands = []
    monkeypatch.setenv("CODE_BRIEFCASE_SESSION_START_NO_BACKGROUND", "1")
    monkeypatch.setattr(
        "code_briefcase.hooks.session._spawn",
        lambda command, *args, **kwargs: commands.append(command),
    )

    response = build_session_start_response(parse_hook_event({"cwd": str(tmp_path)}))

    assert not commands
    assert "created .code-briefcaseignore" in (response.message or "")
    assert "background startup disabled" in (response.message or "")
    assert response.daemon_state is None
