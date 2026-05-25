from __future__ import annotations

from pathlib import Path

import pytest

from code_briefcase.daemon import startup


class FakePidFile:
    def __init__(self) -> None:
        self.closed = False
        self.value = ""

    def fileno(self) -> int:
        return 12345

    def seek(self, *_args) -> None:
        pass

    def truncate(self) -> None:
        self.value = ""

    def write(self, value: str) -> None:
        self.value += value

    def flush(self) -> None:
        pass

    def read(self) -> str:
        return self.value

    def close(self) -> None:
        self.closed = True


def test_start_daemon_uses_live_socket_as_duplicate_guard(monkeypatch, tmp_path, capsys):
    pidfile = FakePidFile()
    monkeypatch.setattr(startup, "_try_acquire_pidfile_lock", lambda _path: pidfile)
    monkeypatch.setattr(startup, "_is_socket_connectable", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("code_briefcase.tldrignore.ensure_tldrignore", lambda _project: (False, ""))

    def fail_if_constructed(_project: Path) -> None:
        raise AssertionError("daemon should not be constructed when a live socket exists")

    monkeypatch.setattr("code_briefcase.daemon.core.TLDRDaemon", fail_if_constructed)

    startup.start_daemon(tmp_path)

    assert "Daemon already running" in capsys.readouterr().out
    assert pidfile.closed


def test_unix_parent_does_not_unlock_child_pidfile_after_fork(monkeypatch, tmp_path):
    pidfile = FakePidFile()
    socket_path = tmp_path / "daemon.sock"
    socket_path.touch()
    connectable = iter([False, True])
    flock_calls = []

    class FakeDaemon:
        def __init__(self, project: Path) -> None:
            self.project = project
            self.socket_path = socket_path

    monkeypatch.setattr(startup, "_try_acquire_pidfile_lock", lambda _path: pidfile)
    monkeypatch.setattr(startup, "_is_socket_connectable", lambda *_args, **_kwargs: next(connectable))
    monkeypatch.setattr(startup, "_get_socket_path", lambda _project: socket_path)
    monkeypatch.setattr("code_briefcase.tldrignore.ensure_tldrignore", lambda _project: (False, ""))
    monkeypatch.setattr("code_briefcase.daemon.core.TLDRDaemon", FakeDaemon)
    monkeypatch.setattr(startup.os, "fork", lambda: 12345)
    monkeypatch.setattr(startup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(startup.fcntl, "flock", lambda _fd, op: flock_calls.append(op))

    startup.start_daemon(tmp_path)

    assert pidfile.closed
    assert startup.fcntl.LOCK_UN not in flock_calls


def test_daemon_child_stdio_redirects_to_devnull(monkeypatch):
    calls = []

    monkeypatch.setattr(startup.os, "open", lambda path, flags: calls.append(("open", path, flags)) or 99)
    monkeypatch.setattr(startup.os, "dup2", lambda src, dst: calls.append(("dup2", src, dst)))
    monkeypatch.setattr(startup.os, "close", lambda fd: calls.append(("close", fd)))

    startup._redirect_standard_streams_to_devnull()

    assert calls[0][0] == "open"
    assert ("dup2", 99, 0) in calls
    assert ("dup2", 99, 1) in calls
    assert ("dup2", 99, 2) in calls
    assert ("close", 99) in calls


def test_configure_daemon_file_logging_writes_errors(tmp_path):
    log_path = startup._get_daemon_log_path(tmp_path)
    startup._configure_daemon_file_logging(tmp_path)
    import logging

    logging.getLogger("code_briefcase.daemon.test").error("daemon child failure")
    logging.shutdown()

    assert log_path.exists()
    assert "daemon child failure" in log_path.read_text()


def test_fork_child_configures_file_logging(monkeypatch, tmp_path):
    pidfile = FakePidFile()
    configured = []

    class FakeDaemon:
        def __init__(self, project: Path) -> None:
            self.project = project
            self._pidfile = None

        def run(self) -> None:
            startup.sys.exit(0)

    monkeypatch.setattr(startup, "_try_acquire_pidfile_lock", lambda _path: pidfile)
    monkeypatch.setattr(startup, "_is_socket_connectable", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("code_briefcase.tldrignore.ensure_tldrignore", lambda _project: (False, ""))
    monkeypatch.setattr("code_briefcase.daemon.core.TLDRDaemon", FakeDaemon)
    monkeypatch.setattr(
        startup,
        "_configure_daemon_file_logging",
        lambda project: configured.append(project),
    )
    monkeypatch.setattr(startup.os, "fork", lambda: 0)
    monkeypatch.setattr(startup.os, "setsid", lambda: None)
    monkeypatch.setattr(startup, "_redirect_standard_streams_to_devnull", lambda: None)
    monkeypatch.setattr(startup.sys, "exit", lambda _code: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        startup.start_daemon(tmp_path)

    assert configured == [tmp_path.resolve()]
