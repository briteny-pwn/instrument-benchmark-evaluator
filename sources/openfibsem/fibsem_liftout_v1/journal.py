from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence


JOURNAL_SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
MAX_CANONICAL_BYTES = 1_048_576


class JournalError(ValueError):
    """The audit journal is malformed, truncated, or has a broken hash chain."""


def _plain(value: object, path: str = "$") -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JournalError(f"non-finite number at {path}")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in result:
                raise JournalError(f"invalid object key at {path}")
            result[key] = _plain(item, f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(item, f"{path}[]") for item in value]
    raise JournalError(f"unsupported journal value at {path}: {type(value).__name__}")


def canonical_bytes(value: object) -> bytes:
    payload = json.dumps(
        _plain(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    if len(payload) > MAX_CANONICAL_BYTES:
        raise JournalError("canonical value is too large")
    return payload


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _event_hash(record_without_hash: Mapping[str, object]) -> str:
    return canonical_digest(record_without_hash)


@dataclass(frozen=True)
class JournalEvent:
    sequence: int
    run_id: str
    world_id: str
    recorded_ns: int
    kind: str
    fields: Mapping[str, object]
    previous_hash: str
    event_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "world_id": self.world_id,
            "recorded_ns": self.recorded_ns,
            "kind": self.kind,
            "fields": _plain(self.fields),
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
        }


class EventJournal:
    def __init__(self, run_id: str, world_id: str):
        if not run_id or not world_id:
            raise ValueError("journal run and world identities must not be empty")
        self.run_id = run_id
        self.world_id = world_id
        self._events: list[JournalEvent] = []

    @classmethod
    def from_records(
        cls,
        records: Sequence[Mapping[str, object]],
        *,
        run_id: str,
        world_id: str,
        require_terminal: bool = False,
    ) -> "EventJournal":
        validate_records(
            records,
            run_id,
            world_id,
            require_terminal=require_terminal,
        )
        journal = cls(run_id, world_id)
        journal._events = [
            JournalEvent(
                sequence=int(record["sequence"]),
                run_id=run_id,
                world_id=world_id,
                recorded_ns=int(record["recorded_ns"]),
                kind=str(record["kind"]),
                fields=MappingProxyType(dict(record["fields"])),  # type: ignore[arg-type]
                previous_hash=str(record["previous_hash"]),
                event_hash=str(record["event_hash"]),
            )
            for record in records
        ]
        return journal

    @property
    def events(self) -> tuple[JournalEvent, ...]:
        return tuple(self._events)

    @property
    def head_hash(self) -> str:
        return self._events[-1].event_hash if self._events else GENESIS_HASH

    @property
    def sequence(self) -> int:
        return len(self._events)

    def append(self, kind: str, **fields: object) -> JournalEvent:
        if not kind or len(kind) > 128:
            raise JournalError("journal event kind is invalid")
        plain_fields = _plain(fields)
        assert isinstance(plain_fields, dict)
        base: dict[str, object] = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "sequence": len(self._events) + 1,
            "run_id": self.run_id,
            "world_id": self.world_id,
            "recorded_ns": time.monotonic_ns(),
            "kind": kind,
            "fields": plain_fields,
            "previous_hash": self.head_hash,
        }
        digest = _event_hash(base)
        event = JournalEvent(
            sequence=base["sequence"],  # type: ignore[arg-type]
            run_id=self.run_id,
            world_id=self.world_id,
            recorded_ns=base["recorded_ns"],  # type: ignore[arg-type]
            kind=kind,
            fields=MappingProxyType(plain_fields),
            previous_hash=self.head_hash,
            event_hash=digest,
        )
        self._events.append(event)
        return event

    def export(self, root: Path) -> tuple[Path, Path]:
        destination = Path(root)
        destination.mkdir(parents=True, exist_ok=True)
        records = [event.to_dict() for event in self._events]
        validate_records(records, self.run_id, self.world_id)
        jsonl_payload = b"".join(canonical_bytes(record) + b"\n" for record in records)
        summary_payload = canonical_bytes(
            {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "run_id": self.run_id,
                "world_id": self.world_id,
                "event_count": len(records),
                "head_hash": self.head_hash,
            }
        ) + b"\n"
        jsonl = destination / "journal.jsonl"
        summary = destination / "journal-summary.json"
        _atomic_write(jsonl, jsonl_payload)
        _atomic_write(summary, summary_payload)
        return jsonl, summary


def validate_records(
    records: Sequence[Mapping[str, object]],
    run_id: str,
    world_id: str,
    *,
    require_terminal: bool = False,
) -> str:
    previous = GENESIS_HASH
    previous_timestamp = 0
    required = {
        "schema_version",
        "sequence",
        "run_id",
        "world_id",
        "recorded_ns",
        "kind",
        "fields",
        "previous_hash",
        "event_hash",
    }
    for expected_sequence, source in enumerate(records, start=1):
        record = _plain(source)
        if not isinstance(record, dict) or set(record) != required:
            raise JournalError("journal event fields are invalid")
        if record["run_id"] != run_id or record["world_id"] != world_id:
            raise JournalError("journal identity mismatch")
        if record["schema_version"] != JOURNAL_SCHEMA_VERSION:
            raise JournalError("journal schema version mismatch")
        if (
            isinstance(record["sequence"], bool)
            or not isinstance(record["sequence"], int)
            or record["sequence"] != expected_sequence
        ):
            raise JournalError("journal sequence mismatch")
        if (
            isinstance(record["recorded_ns"], bool)
            or not isinstance(record["recorded_ns"], int)
            or record["recorded_ns"] <= 0
            or record["recorded_ns"] < previous_timestamp
        ):
            raise JournalError("journal timestamp is invalid")
        if not isinstance(record["kind"], str) or not record["kind"]:
            raise JournalError("journal event kind is invalid")
        if not isinstance(record["fields"], dict):
            raise JournalError("journal event fields are invalid")
        if not _is_digest(record["previous_hash"]) or not _is_digest(
            record["event_hash"]
        ):
            raise JournalError("journal event digest is invalid")
        if record["previous_hash"] != previous:
            raise JournalError("journal previous hash mismatch")
        claimed = record.pop("event_hash")
        if claimed != _event_hash(record):
            raise JournalError("journal event hash mismatch")
        assert isinstance(claimed, str)
        previous = claimed
        previous_timestamp = record["recorded_ns"]  # type: ignore[assignment]
    if require_terminal and (
        not records or records[-1].get("kind") != "run.terminal"
    ):
        raise JournalError("journal terminal event is missing")
    return previous


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
