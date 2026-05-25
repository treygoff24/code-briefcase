import json
import socket

from code_briefcase.daemon import startup
from code_briefcase.daemon.protocol import (
    DaemonProtocolError,
    DaemonResponseKind,
    decode_response_bytes,
    recv_framed_json,
    recv_json_line,
    send_framed_json,
)
from code_briefcase.daemon.startup import DaemonResponse


def test_framed_json_round_trips_over_socketpair():
    left, right = socket.socketpair()
    try:
        send_framed_json(left, {"status": "ok", "value": "x" * 100})

        assert recv_framed_json(right) == {"status": "ok", "value": "x" * 100}
    finally:
        left.close()
        right.close()


def test_recv_json_line_accepts_crlf():
    left, right = socket.socketpair()
    try:
        left.sendall(b'{"cmd":"status"}\r\n')

        assert recv_json_line(right) == b'{"cmd":"status"}'
    finally:
        left.close()
        right.close()


def test_decode_response_bytes_accepts_legacy_and_framed():
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


def test_query_daemon_response_downgrades_to_legacy(monkeypatch, tmp_path):
    def fail_v2(*_args, **_kwargs):
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


def test_query_daemon_response_reports_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(
        startup,
        "_query_daemon_v2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.timeout("slow daemon")),
    )

    response = startup.query_daemon_response(tmp_path, {"cmd": "status"})

    assert response.kind == DaemonResponseKind.TIMEOUT


def test_query_or_start_daemon_starts_once_when_unreachable(monkeypatch, tmp_path):
    responses = iter(
        [
            DaemonResponse(DaemonResponseKind.UNREACHABLE, message="no socket"),
            DaemonResponse(DaemonResponseKind.OK, payload={"status": "ok"}),
        ]
    )
    starts = []

    monkeypatch.setattr(startup, "query_daemon_response", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(
        startup,
        "start_daemon",
        lambda project, **kwargs: starts.append((project, kwargs)),
    )

    response = startup.query_or_start_daemon(tmp_path, {"cmd": "status"})

    assert response.kind == DaemonResponseKind.OK
    assert response.payload == {"status": "ok"}
    assert starts == [(tmp_path, {"quiet": True})]
