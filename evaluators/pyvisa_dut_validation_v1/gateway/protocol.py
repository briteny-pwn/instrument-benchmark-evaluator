from __future__ import annotations

import json
import socket
import struct
from typing import Any


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1_048_576


class ProtocolError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class GatewayError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("socket closed during frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(
    sock: socket.socket, *, max_bytes: int = MAX_FRAME_BYTES
) -> dict[str, Any]:
    size = struct.unpack(">I", recv_exact(sock, 4))[0]
    if size > max_bytes:
        raise ProtocolError("frame_too_large", f"{size} > {max_bytes}")
    try:
        value = json.loads(recv_exact(sock, size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid_json", str(exc)) from exc
    if not isinstance(value, dict):
        raise ProtocolError("invalid_envelope", "frame must be a JSON object")
    return value


def send_frame(
    sock: socket.socket, value: dict[str, Any], *, max_bytes: int = MAX_FRAME_BYTES
) -> None:
    payload = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(payload) > max_bytes:
        raise ProtocolError("frame_too_large", f"{len(payload)} > {max_bytes}")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def validate_request(value: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if value.get("version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_version")
    request_id = value.get("request_id")
    operation = value.get("operation")
    arguments = value.get("arguments", {})
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError("invalid_request_id")
    if not isinstance(operation, str) or not operation:
        raise ProtocolError("invalid_operation")
    if not isinstance(arguments, dict):
        raise ProtocolError("invalid_arguments")
    return request_id, operation, arguments
