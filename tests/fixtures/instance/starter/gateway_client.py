from __future__ import annotations

import base64
import socket
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 1


class GatewayError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class GatewayClient:
    def __init__(self, endpoint: str | Path):
        self.endpoint = str(endpoint)
        self._socket: socket.socket | None = None
        self._request_number = 0

    def __enter__(self) -> "GatewayClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def connect(self) -> None:
        if self._socket is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.endpoint)
        self._socket = sock

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def protocol_version(self) -> int:
        return int(self._request("protocol_version"))

    def list_resources(self) -> tuple[str, ...]:
        return tuple(self._request("list_resources"))

    def open_resource(self, resource: str) -> str:
        return str(self._request("open_resource", resource=resource))

    def set_timeout(self, session: str, timeout_ms: int) -> None:
        self._request("set_timeout", session=session, timeout_ms=timeout_ms)

    def set_read_termination(self, session: str, termination: str | None) -> None:
        self._request(
            "set_read_termination", session=session, termination=termination
        )

    def set_write_termination(self, session: str, termination: str | None) -> None:
        self._request(
            "set_write_termination", session=session, termination=termination
        )

    def write(self, session: str, payload: bytes) -> int:
        result = self._request(
            "write",
            session=session,
            payload_b64=base64.b64encode(payload).decode("ascii"),
        )
        return int(result["bytes_written"])

    def read(self, session: str) -> bytes:
        result = self._request("read", session=session)
        return base64.b64decode(result["payload_b64"], validate=True)

    def close_resource(self, session: str) -> None:
        self._request("close", session=session)

    def _request(self, operation: str, **arguments: Any) -> Any:
        if self._socket is None:
            self.connect()
        assert self._socket is not None
        self._request_number += 1
        request_id = f"request-{self._request_number}"
        value = {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "operation": operation,
            "arguments": arguments,
        }
        _send_frame(self._socket, value)
        response = _recv_frame(self._socket)
        if response.get("request_id") != request_id:
            raise GatewayError("response_mismatch", "request ID does not match")
        if response.get("version") != PROTOCOL_VERSION:
            raise GatewayError("response_version", "unsupported response version")
        if not response.get("ok"):
            error = response.get("error", {})
            raise GatewayError(
                str(error.get("code", "gateway_error")),
                str(error.get("message", "gateway request failed")),
            )
        return response.get("result")


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = sock.recv(size)
        if not chunk:
            raise GatewayError("connection_closed", "gateway closed the connection")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _send_frame(sock: socket.socket, value: dict[str, Any]) -> None:
    import json
    import struct

    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _recv_frame(sock: socket.socket) -> dict[str, Any]:
    import json
    import struct

    size = struct.unpack(">I", _recv_exact(sock, 4))[0]
    if size > 1_048_576:
        raise GatewayError("frame_too_large", "gateway response is too large")
    value = json.loads(_recv_exact(sock, size))
    if not isinstance(value, dict):
        raise GatewayError("invalid_response", "gateway response is not an object")
    return value
