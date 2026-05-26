from typing import Any
import json
import socket
import time

import pytest

from code_briefcase.daemon import startup
from code_briefcase.daemon.core import TLDRDaemon
from code_briefcase.daemon.protocol import (
    DaemonProtocolError,
    DaemonResponseKind,
    LineReader,
    decode_response_bytes,
    recv_framed_json,
    recv_json_line,
    send_framed_json,
)
from code_briefcase.daemon.startup import DaemonResponse, query_daemon


def test_framed_json_round_trips_over_socketpair() -> None:
    left, right = socket.socketpair()
    try:
        send_framed_json(left, {"status": "ok", "value": "x" * 100})

        assert recv_framed_json(right) == {"status": "ok", "value": "x" * 100}
    finally:
        left.close()
        right.close()


def test_recv_json_line_accepts_crlf() -> None:
    left, right = socket.socketpair()
    try:
        left.sendall(b'{"cmd":"status"}\r\n')

        assert recv_json_line(right) == b'{"cmd":"status"}'
    finally:
        left.close()
        right.close()


def test_decode_response_bytes_accepts_legacy_and_framed() -> None:
    legacy = json.dumps({"status": "ok", "mode": "legacy"}).encode()

    left, right = socket.socketpair()
    try:
        send_framed_json(left, {"status": "ok", "mode": "framed"})
        framed = right.recv(4), right.recv(4096)
    finally:
        left.close()
        right.close()

    assert decode_response_bytes([legacy]) == {"status": "ok", "mode": "legacy"}
    assert decode_response_bytes(list(framed)) == {"status": "ok", "mode": "framed"}


def test_query_daemon_response_downgrades_to_legacy(
    monkeypatch: Any, tmp_path: Any
) -> None:
    def fail_v2(*_args: Any, **_kwargs: Any) -> None:
        raise DaemonProtocolError("old daemon")

    monkeypatch.setattr(startup, "_query_daemon_v2", fail_v2)
    monkeypatch.setattr(
        startup,
        "_query_daemon_legacy",
        lambda *_args, **_kwargs: {"status": "ok", "protocol": "legacy"},
    )

    response = startup.query_daemon_response(tmp_path, {"cmd": "status"})

    assert response.kind == DaemonResponseKind.OK
    assert response.payload == {"status": "ok", "protocol": "legacy"}


def test_query_daemon_response_reports_timeout(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr(
        startup,
        "_query_daemon_v2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.timeout("slow daemon")),
    )

    response = startup.query_daemon_response(tmp_path, {"cmd": "status"})

    assert response.kind == DaemonResponseKind.TIMEOUT


def test_daemon_response_ok_rejects_application_errors() -> None:
    assert not DaemonResponse(
        DaemonResponseKind.OK,
        payload={"status": "error", "message": "bad command"},
    ).ok
    assert not DaemonResponse(
        DaemonResponseKind.OK,
        payload={"status": "shutting_down"},
    ).ok
    assert DaemonResponse(DaemonResponseKind.OK, payload={"status": "ok"}).ok
    assert DaemonResponse(DaemonResponseKind.OK, payload={"result": "x"}).ok


def test_query_daemon_raises_with_application_error_message(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setattr(
        startup,
        "query_daemon_response",
        lambda *_args, **_kwargs: DaemonResponse(
            DaemonResponseKind.OK,
            payload={"status": "error", "message": "Unknown command: nope"},
        ),
    )

    with pytest.raises(RuntimeError, match="Unknown command: nope"):
        query_daemon(tmp_path, {"cmd": "nope"})


def test_line_reader_handles_coalesced_messages() -> None:
    left, right = socket.socketpair()
    reader = LineReader()
    try:
        left.sendall(b'{"cmd":"hello"}\n{"cmd":"status"}\n')

        assert reader.readline(right) == b'{"cmd":"hello"}'
        assert reader.readline(right) == b'{"cmd":"status"}'
    finally:
        left.close()
        right.close()


def test_query_or_start_daemon_starts_once_when_unreachable(
    monkeypatch: Any, tmp_path: Any
) -> None:
    responses = iter(
        [
            DaemonResponse(DaemonResponseKind.UNREACHABLE, message="no socket"),
            DaemonResponse(DaemonResponseKind.OK, payload={"status": "ok"}),
        ]
    )
    starts = []

    monkeypatch.setattr(
        startup, "query_daemon_response", lambda *_args, **_kwargs: next(responses)
    )
    monkeypatch.setattr(
        startup,
        "start_daemon",
        lambda project, **kwargs: starts.append((project, kwargs)),
    )

    response = startup.query_or_start_daemon(tmp_path, {"cmd": "status"})

    assert response.kind == DaemonResponseKind.OK
    assert response.payload == {"status": "ok"}
    assert starts == [(tmp_path, {"quiet": True})]


def test_shutdown_ack_returns_before_slow_supervisor_teardown(
    tmp_path: Any, monkeypatch: Any
) -> None:
    daemon = TLDRDaemon(tmp_path)

    def slow_stop() -> None:
        time.sleep(2.0)

    monkeypatch.setattr(daemon, "_stop_watch_supervisor", slow_stop)

    start = time.monotonic()
    response = daemon.handle_command({"cmd": "shutdown"})
    elapsed_ms = (time.monotonic() - start) * 1000

    assert response["status"] == "shutting_down"
    assert response["cleanup_in_progress"] is True
    assert elapsed_ms < 500
