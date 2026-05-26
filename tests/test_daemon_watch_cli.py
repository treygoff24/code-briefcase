from __future__ import annotations
from typing import Any

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from code_briefcase.daemon.protocol import DaemonResponseKind
from code_briefcase.daemon.startup import DaemonResponse


def test_daemon_watchers_status_cli_outputs_json(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from code_briefcase import cli

    monkeypatch.setattr(
        "code_briefcase.daemon.query_daemon_response",
        lambda _project, command: DaemonResponse(
            DaemonResponseKind.OK,
            payload={"status": "ok", "watchers": [], "count": 0, "command": command},
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "code-briefcase",
            "daemon",
            "watchers",
            "status",
            "--project",
            str(tmp_path),
            "--json",
        ],
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        cli.main()

    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "ok"
    assert payload["command"] == {"cmd": "watchers", "action": "status"}


def test_daemon_watchers_start_cli_sends_start_command(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from code_briefcase import cli

    source = tmp_path / "app.ts"
    source.write_text("const answer = 42;\n", encoding="utf-8")
    seen = {}

    def fake_query(project: Any, command: Any, **_kwargs: Any) -> Any:
        seen["project"] = project
        seen["command"] = command
        return DaemonResponse(
            DaemonResponseKind.OK,
            payload={"status": "ok", "watcher_status": "pending"},
        )

    monkeypatch.setattr("code_briefcase.daemon.query_or_start_daemon", fake_query)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "code-briefcase",
            "daemon",
            "watchers",
            "start",
            str(source),
            "--project",
            str(tmp_path),
            "--lang",
            "typescript",
            "--json",
        ],
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        cli.main()

    assert seen["project"] == tmp_path.resolve()
    assert seen["command"]["cmd"] == "watchers"
    assert seen["command"]["action"] == "start"
    assert seen["command"]["file"] == str(source.resolve())
    assert seen["command"]["language"] == "typescript"
    assert json.loads(stdout.getvalue())["watcher_status"] == "pending"


def test_daemon_status_cli_reports_timeout(tmp_path: Any, monkeypatch: Any) -> None:
    from code_briefcase import cli

    monkeypatch.setattr(
        "code_briefcase.daemon.query_daemon_response",
        lambda *_args, **_kwargs: DaemonResponse(
            DaemonResponseKind.TIMEOUT, message="slow"
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["code-briefcase", "daemon", "status", "--project", str(tmp_path)],
    )

    stderr = io.StringIO()
    with redirect_stderr(stderr), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "timed out" in stderr.getvalue().lower()


def test_daemon_watchers_start_cli_resolves_relative_file_against_project(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from code_briefcase import cli

    project = tmp_path / "project"
    project.mkdir()
    source = project / "app.ts"
    source.write_text("const answer = 42;\n", encoding="utf-8")
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    seen = {}

    def fake_query(project: Any, command: Any, **_kwargs: Any) -> Any:
        seen["project"] = project
        seen["command"] = command
        return DaemonResponse(
            DaemonResponseKind.OK,
            payload={"status": "ok", "watcher_status": "pending"},
        )

    monkeypatch.setattr("code_briefcase.daemon.query_or_start_daemon", fake_query)
    monkeypatch.chdir(other_cwd)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "code-briefcase",
            "daemon",
            "watchers",
            "start",
            "app.ts",
            "--project",
            str(project),
            "--json",
        ],
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        cli.main()

    assert seen["project"] == project.resolve()
    assert seen["command"]["file"] == str(source.resolve())
    assert json.loads(stdout.getvalue())["watcher_status"] == "pending"


def test_daemon_watchers_start_cli_surfaces_application_error(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from code_briefcase import cli

    source = tmp_path / "app.ts"
    source.write_text("const answer = 42;\n", encoding="utf-8")

    monkeypatch.setattr(
        "code_briefcase.daemon.query_or_start_daemon",
        lambda *_args, **_kwargs: DaemonResponse(
            DaemonResponseKind.OK,
            payload={"status": "error", "message": "watcher budget exceeded"},
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "code-briefcase",
            "daemon",
            "watchers",
            "start",
            str(source),
            "--project",
            str(tmp_path),
        ],
    )

    stderr = io.StringIO()
    with redirect_stderr(stderr), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "watcher budget exceeded" in stderr.getvalue()
