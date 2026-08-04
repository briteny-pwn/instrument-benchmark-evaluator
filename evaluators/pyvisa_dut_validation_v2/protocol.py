from __future__ import annotations

import base64
import binascii
import json
import math
import socket
import threading
from collections.abc import Callable, Mapping
from typing import Any, TypeAlias


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1_048_576
WireValue: TypeAlias = (
    None | bool | int | float | str | bytes | tuple["WireValue", ...]
)


class ProtocolError(ValueError):
    """The peer sent data outside the closed public wire protocol."""


class RemoteVisaError(Exception):
    """A controlled error response returned by the trusted broker."""

    def __init__(self, kind: str, code: int, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message


def encode_wire_value(value: WireValue) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError("wire floats must be finite")
        return value
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, tuple):
        return {
            "type": "list",
            "items": [encode_wire_value(item) for item in value],
        }
    raise ProtocolError("unsupported wire value")


def decode_wire_value(value: object) -> WireValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError("wire floats must be finite")
        return value
    if not isinstance(value, dict):
        raise ProtocolError("unsupported wire value")
    tag = value.get("type")
    if tag == "bytes":
        if set(value) != {"type", "base64"} or not isinstance(
            value["base64"], str
        ):
            raise ProtocolError("invalid bytes value")
        try:
            return base64.b64decode(value["base64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProtocolError("invalid base64 value") from exc
    if tag == "list":
        if set(value) != {"type", "items"} or not isinstance(
            value["items"], list
        ):
            raise ProtocolError("invalid list value")
        return tuple(decode_wire_value(item) for item in value["items"])
    raise ProtocolError("unknown wire value tag")


def encode_request(
    request_id: int,
    operation: str,
    args: Mapping[str, WireValue],
) -> bytes:
    if not _valid_request_id(request_id):
        raise ProtocolError("invalid request ID")
    if not isinstance(operation, str) or not operation:
        raise ProtocolError("invalid operation")
    if not isinstance(args, Mapping) or not all(
        isinstance(key, str) and key for key in args
    ):
        raise ProtocolError("invalid request arguments")
    return encode_message(
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "operation": operation,
            "args": {
                key: encode_wire_value(value) for key, value in args.items()
            },
        }
    )


def decode_request(
    message: Mapping[str, object],
) -> tuple[int, str, dict[str, WireValue]]:
    if not isinstance(message, Mapping) or set(message) != {
        "version",
        "request_id",
        "operation",
        "args",
    }:
        raise ProtocolError("invalid request shape")
    if (
        isinstance(message["version"], bool)
        or not isinstance(message["version"], int)
        or message["version"] != PROTOCOL_VERSION
    ):
        raise ProtocolError("unsupported protocol version")
    request_id = message["request_id"]
    if not _valid_request_id(request_id):
        raise ProtocolError("invalid request ID")
    operation = message["operation"]
    if not isinstance(operation, str) or not operation:
        raise ProtocolError("invalid operation")
    raw_args = message["args"]
    if not isinstance(raw_args, dict) or not all(
        isinstance(key, str) and key for key in raw_args
    ):
        raise ProtocolError("invalid request arguments")
    return (
        request_id,
        operation,
        {key: decode_wire_value(value) for key, value in raw_args.items()},
    )


def decode_response(
    message: Mapping[str, object], expected_request_id: int
) -> tuple[WireValue, int | None]:
    if not isinstance(message, Mapping):
        raise ProtocolError("invalid response shape")
    common = {"version", "request_id", "ok"}
    if not common.issubset(message):
        raise ProtocolError("invalid response shape")
    if (
        isinstance(message["version"], bool)
        or not isinstance(message["version"], int)
        or message["version"] != PROTOCOL_VERSION
    ):
        raise ProtocolError("unsupported protocol version")
    request_id = message["request_id"]
    if not _valid_request_id(request_id) or request_id != expected_request_id:
        raise ProtocolError("response request ID mismatch")
    ok = message["ok"]
    if not isinstance(ok, bool):
        raise ProtocolError("invalid response outcome")
    if ok:
        if set(message) != common | {"result", "status"}:
            raise ProtocolError("invalid success response shape")
        status = message["status"]
        if status is not None and (
            isinstance(status, bool) or not isinstance(status, int)
        ):
            raise ProtocolError("invalid VISA status")
        return decode_wire_value(message["result"]), status
    if set(message) != common | {"error"}:
        raise ProtocolError("invalid error response shape")
    error = message["error"]
    if not isinstance(error, dict) or set(error) != {"kind", "code", "message"}:
        raise ProtocolError("invalid remote error")
    kind, code, error_message = error["kind"], error["code"], error["message"]
    if (
        not isinstance(kind, str)
        or not kind
        or isinstance(code, bool)
        or not isinstance(code, int)
        or not isinstance(error_message, str)
        or not error_message
    ):
        raise ProtocolError("invalid remote error")
    raise RemoteVisaError(kind, code, error_message)


def success_response(
    request_id: int, result: WireValue, status: int | None
) -> dict[str, object]:
    if not _valid_request_id(request_id):
        raise ProtocolError("invalid request ID")
    if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
        raise ProtocolError("invalid VISA status")
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": encode_wire_value(result),
        "status": status,
    }


def error_response(
    request_id: int, kind: str, code: int, message: str
) -> dict[str, object]:
    if (
        not _valid_request_id(request_id)
        or not isinstance(kind, str)
        or not kind
        or isinstance(code, bool)
        or not isinstance(code, int)
        or not isinstance(message, str)
        or not message
    ):
        raise ProtocolError("invalid remote error")
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": {"kind": kind, "code": code, "message": message},
    }


def encode_message(message: Mapping[str, object]) -> bytes:
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("message is not canonical JSON") from exc
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("invalid frame length")
    return len(payload).to_bytes(4, "big") + payload


def recv_message(stream: Any) -> dict[str, object]:
    header = _recv_exact(stream, 4)
    length = int.from_bytes(header, "big")
    if length == 0 or length > MAX_FRAME_BYTES:
        raise ProtocolError("invalid frame length")
    payload = _recv_exact(stream, length)
    try:
        text = payload.decode("utf-8")
        message = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON payload") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    return message


def _recv_exact(stream: Any, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise ProtocolError("truncated frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ProtocolError(f"invalid JSON constant: {value}")


def _valid_request_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _connect_unix(endpoint: str) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(endpoint)
    except BaseException:
        connection.close()
        raise
    return connection


class RpcClient:
    """Persistent, serialized client for the public VISA RPC endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        connector: Callable[[str], Any] = _connect_unix,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.startswith("/"):
            raise OSError("IAB_VISA_SOCKET must be an absolute Unix socket path")
        self._endpoint = endpoint
        self._connector = connector
        self._connection: Any | None = None
        self._next_request_id = 1
        self._lock = threading.Lock()

    def call(
        self, operation: str, args: Mapping[str, WireValue]
    ) -> tuple[WireValue, int | None]:
        with self._lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            try:
                if self._connection is None:
                    self._connection = self._connector(self._endpoint)
                self._connection.sendall(
                    encode_request(request_id, operation, args)
                )
                response = recv_message(self._connection)
                return decode_response(response, request_id)
            except RemoteVisaError:
                raise
            except (OSError, ProtocolError):
                self._drop_connection()
                raise

    def close(self) -> None:
        with self._lock:
            self._drop_connection()

    def _drop_connection(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None
