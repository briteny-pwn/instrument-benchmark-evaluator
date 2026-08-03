from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GENESIS_HASH = "0" * 64


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


@dataclass(frozen=True)
class JournalEvent:
    run_id: str
    world_id: str
    sequence: int
    monotonic_ns: int
    previous_hash: str
    kind: str
    fields: dict[str, Any]
    event_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "world_id": self.world_id,
            "sequence": self.sequence,
            "monotonic_ns": self.monotonic_ns,
            "previous_hash": self.previous_hash,
            "kind": self.kind,
            "fields": self.fields,
            "event_hash": self.event_hash,
        }


class EventJournal:
    def __init__(self, run_id: str, world_id: str):
        if not run_id or not world_id:
            raise ValueError("run_id and world_id must not be empty")
        self.run_id = run_id
        self.world_id = world_id
        self._events: list[JournalEvent] = []
        self._lock = threading.Lock()

    @property
    def events(self) -> tuple[JournalEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def final_hash(self) -> str:
        with self._lock:
            return self._final_hash_unlocked()

    def append(self, kind: str, **fields: Any) -> JournalEvent:
        if not isinstance(kind, str) or not kind:
            raise ValueError("event kind must not be empty")
        copied = json.loads(_canonical(fields))
        with self._lock:
            unsigned = {
                "run_id": self.run_id,
                "world_id": self.world_id,
                "sequence": len(self._events) + 1,
                "monotonic_ns": time.monotonic_ns(),
                "previous_hash": self._final_hash_unlocked(),
                "kind": kind,
                "fields": copied,
            }
            event = JournalEvent(
                event_hash=hashlib.sha256(_canonical(unsigned)).hexdigest(),
                **unsigned,
            )
            self._events.append(event)
            return event

    def export(self, path: Path) -> None:
        records = self.events
        payload = b"".join(_canonical(item.to_dict()) + b"\n" for item in records)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _final_hash_unlocked(self) -> str:
        return self._events[-1].event_hash if self._events else GENESIS_HASH
