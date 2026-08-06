from __future__ import annotations

import json
import math
import re
import socket
import struct
from dataclasses import dataclass
from typing import BinaryIO, Mapping, Protocol


MAX_FRAME_BYTES = 1_048_576
PROTOCOL_VERSION = 1
REQUEST_ID = re.compile(r"req-([0-9]{8})\Z")


class ProtocolError(RuntimeError):
    """The candidate sent an invalid, oversized, or replayed RPC frame."""


class ConnectionClosed(ProtocolError):
    """The peer closed a stream before a full frame was received."""


class RejectedPeer(PermissionError):
    """The Unix-socket peer does not have the candidate UID."""


class BrokerVisibleError(RuntimeError):
    """A sanitized operation error that may be returned to the candidate."""


class Dispatcher(Protocol):
    def dispatch(
        self,
        operation: str,
        arguments: dict[str, object],
        *,
        request_id: str | None = None,
    ) -> object: ...


def _plain(value: object, path: str = "$") -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(f"non-finite number at {path}")
        return value
    if isinstance(value, list):
        return [_plain(item, f"{path}[]") for item in value]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in result:
                raise ProtocolError(f"invalid key at {path}")
            result[key] = _plain(item, f"{path}.{key}")
        return result
    raise ProtocolError(f"unsupported value at {path}")


def _object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate key: {key}")
        result[key] = value
    return result


def encode_payload(value: Mapping[str, object]) -> bytes:
    plain = _plain(dict(value))
    payload = json.dumps(
        plain,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    return payload


def decode_payload(payload: bytes) -> dict[str, object]:
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ProtocolError(f"non-finite JSON number: {token}")
            ),
            object_pairs_hook=_object_from_pairs,
        )
    except ProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("frame root must be an object")
    plain = _plain(value)
    assert isinstance(plain, dict)
    return plain


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ConnectionClosed("truncated frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO) -> dict[str, object]:
    length = struct.unpack("!I", _read_exact(stream, 4))[0]
    if length > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    return decode_payload(_read_exact(stream, length))


def write_frame(stream: BinaryIO, value: Mapping[str, object]) -> None:
    payload = encode_payload(value)
    stream.write(struct.pack("!I", len(payload)))
    stream.write(payload)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


@dataclass
class BrokerSession:
    dispatcher: Dispatcher
    next_request_number: int = 1

    def process(self, request: Mapping[str, object]) -> dict[str, object]:
        value = _plain(dict(request))
        assert isinstance(value, dict)
        if set(value) != {"protocol_version", "request_id", "operation", "arguments"}:
            raise ProtocolError("request fields are invalid")
        if value["protocol_version"] != PROTOCOL_VERSION:
            raise ProtocolError("request protocol version mismatch")
        request_id = value["request_id"]
        if not isinstance(request_id, str):
            raise ProtocolError("request ID is invalid")
        match = REQUEST_ID.fullmatch(request_id)
        if match is None or int(match.group(1)) != self.next_request_number:
            raise ProtocolError("request replay or order violation")
        operation, arguments = value["operation"], value["arguments"]
        if (
            not isinstance(operation, str)
            or not operation
            or operation.startswith("_")
            or len(operation) > 128
        ):
            raise ProtocolError("operation name is invalid")
        if not isinstance(arguments, dict):
            raise ProtocolError("request arguments must be an object")
        self.next_request_number += 1
        try:
            result = self.dispatcher.dispatch(
                operation, arguments, request_id=request_id
            )
            response: dict[str, object] = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
        except BrokerVisibleError as exc:
            response = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": False,
                "error": {"code": "rejected_operation", "message": str(exc)[:512]},
            }
        except Exception:
            response = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "trusted operation failed",
                },
            }
        encode_payload(response)
        return response


class FibsemBroker:
    def __init__(self, dispatcher: Dispatcher, *, expected_peer_uid: int = 10001):
        if expected_peer_uid < 1:
            raise ValueError("expected peer UID must be positive")
        self.dispatcher = dispatcher
        self.expected_peer_uid = expected_peer_uid

    def open_session(self, *, peer_uid: int) -> BrokerSession:
        if peer_uid != self.expected_peer_uid:
            raise RejectedPeer(f"peer UID must be {self.expected_peer_uid}")
        return BrokerSession(self.dispatcher)

    def serve_connection(self, connection: socket.socket) -> None:
        session = self.open_session(peer_uid=_linux_peer_uid(connection))
        stream = connection.makefile("rwb", buffering=0)
        try:
            while True:
                try:
                    request = read_frame(stream)
                except ConnectionClosed:
                    return
                write_frame(stream, session.process(request))
        finally:
            stream.close()


def _linux_peer_uid(connection: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise RejectedPeer("Linux SO_PEERCRED is required")
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    if not isinstance(credentials, bytes) or len(credentials) != 12:
        raise RejectedPeer("peer credentials are unavailable")
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid
