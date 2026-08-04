from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from ..models import WorldSnapshot


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _state(snapshot: WorldSnapshot) -> tuple[dict[str, Any], str]:
    value = asdict(snapshot)
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return value, _digest(encoded)


@dataclass(frozen=True)
class EvidenceEvent:
    run_id: str
    world_id: str
    sequence: int
    monotonic_ns: int
    operation: str
    session_digest: str | None
    resource: str | None
    role: str | None
    request_b64: str | None
    response_b64: str | None
    request_sha256: str
    response_sha256: str
    request_bytes: int
    response_bytes: int
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    state_before_sha256: str
    state_after_sha256: str
    outcome: str
    error_code: str | None
    cleanup_source: str | None
    source_sequence: int | None = None


class EventJournal:
    def __init__(self, run_id: str, world_id: str):
        self.run_id = run_id
        self.world_id = world_id
        self._events: list[EvidenceEvent] = []
        self._lock = threading.Lock()

    def append(
        self,
        *,
        operation: str,
        state_before: WorldSnapshot,
        state_after: WorldSnapshot,
        session: str | None = None,
        resource: str | None = None,
        role: str | None = None,
        request: bytes | None = None,
        response: bytes | None = None,
        outcome: str = "ok",
        error_code: str | None = None,
        cleanup_source: str | None = None,
    ) -> EvidenceEvent:
        request_payload = request or b""
        response_payload = response or b""
        before_value, before_digest = _state(state_before)
        after_value, after_digest = _state(state_after)
        with self._lock:
            record = EvidenceEvent(
                run_id=self.run_id,
                world_id=self.world_id,
                sequence=len(self._events) + 1,
                monotonic_ns=time.monotonic_ns(),
                operation=operation,
                session_digest=(
                    _digest(session.encode("utf-8")) if session is not None else None
                ),
                resource=resource,
                role=role,
                request_b64=(
                    base64.b64encode(request).decode("ascii")
                    if request is not None
                    else None
                ),
                response_b64=(
                    base64.b64encode(response).decode("ascii")
                    if response is not None
                    else None
                ),
                request_sha256=_digest(request_payload),
                response_sha256=_digest(response_payload),
                request_bytes=len(request_payload),
                response_bytes=len(response_payload),
                state_before=before_value,
                state_after=after_value,
                state_before_sha256=before_digest,
                state_after_sha256=after_digest,
                outcome=outcome,
                error_code=error_code,
                cleanup_source=cleanup_source,
            )
            self._events.append(record)
            return record

    def events(self) -> tuple[EvidenceEvent, ...]:
        with self._lock:
            return tuple(self._events)
