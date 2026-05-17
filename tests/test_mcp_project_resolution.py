import os
import sys

import pytest

from tldr import mcp_server


def test_explicit_path_wins(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("TLDR_PROJECT", str(other))

    assert mcp_server._resolve_project(str(tmp_path)) == str(tmp_path.resolve())


def test_tldr_project_wins_over_pwd(tmp_path, monkeypatch):
    project = tmp_path / "project"
    pwd = tmp_path / "pwd"
    project.mkdir()
    pwd.mkdir()
    monkeypatch.setenv("TLDR_PROJECT", str(project))
    monkeypatch.setenv("PWD", str(pwd))

    assert mcp_server._resolve_project("auto") == str(project.resolve())


def test_missing_env_falls_back_to_cwd(tmp_path, monkeypatch):
    for key in ("TLDR_PROJECT", "CLAUDE_PROJECT_DIR", "CODEX_PROJECT_DIR", "CODEX_CWD", "PWD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)

    assert mcp_server._resolve_project("auto") == str(tmp_path.resolve())


def test_nonexistent_explicit_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        mcp_server._resolve_project(str(tmp_path / "missing"))


def test_tool_functions_call_resolve_project(tmp_path, monkeypatch):
    calls = {}

    def fake_send(project, command):
        calls["project"] = project
        calls["command"] = command
        return {"status": "ok"}

    monkeypatch.setattr(mcp_server, "_send_command", fake_send)

    mcp_server.status(project=str(tmp_path))

    assert calls["project"] == str(tmp_path.resolve())


def test_project_auto_does_not_set_tldr_project_to_auto(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.delenv("TLDR_PROJECT", raising=False)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda transport: seen.setdefault("ran", transport))
    monkeypatch.setattr(sys, "argv", ["tldr-mcp", "--project", "auto"])
    monkeypatch.chdir(tmp_path)

    mcp_server.main()

    assert os.environ.get("TLDR_PROJECT") is None
    assert seen["ran"] == "stdio"


def test_explicit_nonexistent_project_raises_even_when_pwd_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("PWD", str(tmp_path))

    with pytest.raises(FileNotFoundError):
        mcp_server._resolve_project(str(tmp_path / "missing"))


def test_relative_file_tool_args_resolve_against_project_root(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "app.py"
    source.write_text("def main():\n    return 1\n")
    calls = {}

    def fake_send(project, command):
        calls["project"] = project
        calls["command"] = command
        return {"status": "ok"}

    monkeypatch.setattr(mcp_server, "_send_command", fake_send)

    mcp_server.extract("src/app.py", project=str(tmp_path))

    assert calls["project"] == str(tmp_path.resolve())
    assert calls["command"]["file"] == str(source.resolve())
