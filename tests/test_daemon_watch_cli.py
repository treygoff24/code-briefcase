from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout

from code_briefcase.daemon.protocol import DaemonResponseKind
from code_briefcase.daemon.startup import DaemonResponse


def test_daemon_watchers_status_cli_outputs_json(tmp_path, monkeypatch):
    from code_briefcase import cli

    monkeypatch.setattr(
        "code_briefcase.daemon.query_daemon",
        lambda _project, command: {"status": "ok", "watchers": [], "count": 0, "command": command},
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


def test_daemon_watchers_start_cli_sends_start_command(tmp_path, monkeypatch):
    from code_briefcase import cli

    source = tmp_path / "app.ts"
    source.write_text("const answer = 42;\n", encoding="utf-8")
    seen = {}

    def fake_query(project, command, **_kwargs):
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
