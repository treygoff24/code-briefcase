"""Daemon socket protocol helpers.

Protocol v1 is newline-delimited JSON for request and response.
Protocol v2 keeps newline-delimited JSON requests, but responses are framed as:

    8-byte big-endian payload length + UTF-8 JSON payload

The request shape stays line-based so legacy clients can continue talking to a
new daemon. A v2 client first sends ``{"cmd": "hello", "protocol_version": 2}``
and receives a framed acknowledgement before sending the command on the same
connection.
"""

from __future__ import annotations

import json
import socket
from enum import Enum
from struct import pack, unpack
from typing import Any

PROTOCOL_VERSION = 2
FRAME_HEADER_BYTES = 8
MAX_FRAME_BYTES = 16 * 1024 * 1024


class DaemonResponseKind(str, Enum):
    OK = "ok"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    FALLBACK_REQUIRED = "fallback_required"


class DaemonProtocolError(RuntimeError):
    """Raised when daemon bytes do not match the expected protocol."""


class LineReader:
    """Buffered newline reader that preserves bytes after the first line."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def readline(
        self, sock: socket.socket, *, max_bytes: int = MAX_FRAME_BYTES
    ) -> bytes | None:
        while True:
            newline_at = self._buffer.find(b"\n")
            if newline_at >= 0:
                line = bytes(self._buffer[:newline_at]).rstrip(b"\r")
                del self._buffer[: newline_at + 1]
                return line

            chunk = sock.recv(4096)
            if not chunk:
                if not self._buffer:
                    return None
                line = bytes(self._buffer).rstrip(b"\r")
                self._buffer.clear()
                return line

            self._buffer.extend(chunk)
            if len(self._buffer) > max_bytes:
                raise DaemonProtocolError("daemon request exceeded maximum size")


def json_line(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode() + b"\n"


def send_json_line(sock: socket.socket, payload: dict[str, Any]) -> None:
    sock.sendall(json_line(payload))


def send_framed_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload).encode()
    sock.sendall(pack(">Q", len(raw)) + raw)


def recv_json_line(
    sock: socket.socket, *, max_bytes: int = MAX_FRAME_BYTES
) -> bytes | None:
    return LineReader().readline(sock, max_bytes=max_bytes)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise DaemonProtocolError("daemon closed connection mid-frame")
        data.extend(chunk)
    return bytes(data)


def recv_framed_json(
    sock: socket.socket, *, max_bytes: int = MAX_FRAME_BYTES
) -> dict[str, Any]:
    header = recv_exact(sock, FRAME_HEADER_BYTES)
    payload_size = unpack(">Q", header)[0]
    if payload_size > max_bytes:
        raise DaemonProtocolError("daemon response exceeded maximum size")
    payload = recv_exact(sock, payload_size)
    return _decode_json_object(payload.decode())


def recv_legacy_json(
    sock: socket.socket, *, max_bytes: int = MAX_FRAME_BYTES
) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise DaemonProtocolError("daemon response exceeded maximum size")
        try:
            return decode_response_bytes(chunks)
        except json.JSONDecodeError:
            continue
    return decode_response_bytes(chunks)


def decode_response_bytes(chunks: list[bytes]) -> dict[str, Any]:
    raw = b"".join(chunks)
    if len(raw) >= FRAME_HEADER_BYTES:
        payload_size = unpack(">Q", raw[:FRAME_HEADER_BYTES])[0]
        if payload_size == len(raw) - FRAME_HEADER_BYTES:
            return _decode_json_object(raw[FRAME_HEADER_BYTES:].decode())
    return _decode_json_object(raw.decode())


def _decode_json_object(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise DaemonProtocolError("daemon response was not a JSON object")
    return payload
