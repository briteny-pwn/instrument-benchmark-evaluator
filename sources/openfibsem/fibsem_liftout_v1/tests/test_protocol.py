from __future__ import annotations

import io
import struct

import pytest

from sources.openfibsem.fibsem_liftout_v1.protocol import (
    MAX_FRAME_BYTES,
    FibsemBroker,
    ProtocolError,
    RejectedPeer,
    decode_payload,
    read_frame,
)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def dispatch(
        self,
        operation: str,
        arguments: dict[str, object],
        *,
        request_id: str | None = None,
    ) -> object:
        self.calls.append((operation, arguments))
        assert request_id == "req-00000001"
        return {"operation": operation}


def request(number: int = 1) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "request_id": f"req-{number:08d}",
        "operation": "ping",
        "arguments": {},
    }


def test_protocol_rejects_oversize_duplicate_keys_and_truncation() -> None:
    with pytest.raises(ProtocolError, match="too large"):
        read_frame(io.BytesIO(struct.pack("!I", MAX_FRAME_BYTES + 1)))
    with pytest.raises(ProtocolError, match="duplicate"):
        decode_payload(b'{"operation":"ping","operation":"checkpoint"}')
    with pytest.raises(ProtocolError, match="truncated"):
        read_frame(io.BytesIO(struct.pack("!I", 10) + b"{}"))


def test_broker_enforces_peer_uid_request_schema_and_replay_order() -> None:
    dispatcher = RecordingDispatcher()
    broker = FibsemBroker(dispatcher, expected_peer_uid=10001)

    with pytest.raises(RejectedPeer, match="10001"):
        broker.open_session(peer_uid=999)
    session = broker.open_session(peer_uid=10001)
    response = session.process(request())
    assert response["ok"] is True
    assert dispatcher.calls == [("ping", {})]

    with pytest.raises(ProtocolError, match="replay|order"):
        session.process(request())
    malformed = request(2)
    malformed["extra"] = True
    with pytest.raises(ProtocolError, match="fields"):
        session.process(malformed)


def test_broker_returns_bounded_error_without_leaking_exception_details() -> None:
    class FailingDispatcher:
        def dispatch(
            self,
            operation: str,
            arguments: dict[str, object],
            *,
            request_id: str | None = None,
        ) -> object:
            raise ValueError("secret backend path /trusted/simulator")

    session = FibsemBroker(FailingDispatcher(), expected_peer_uid=10001).open_session(
        peer_uid=10001
    )
    response = session.process(request())

    assert response["ok"] is False
    assert response["error"] == {
        "code": "internal_error",
        "message": "trusted operation failed",
    }
