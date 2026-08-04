from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ContainerInfrastructureError


EVENTS_LIMIT = 16 * 1024 * 1024
SUMMARY_LIMIT = 1024 * 1024
FATAL_LIMIT = 64 * 1024
HASH = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SimJournalEvidence:
    events: tuple[dict[str, Any], ...]
    event_count: int
    final_hash: str | None
    pre_cleanup_snapshot: dict[str, Any] | None
    post_cleanup_snapshot: dict[str, Any] | None
    fatal: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [dict(event) for event in self.events],
            "event_count": self.event_count,
            "final_hash": self.final_hash,
            "pre_cleanup_snapshot": self.pre_cleanup_snapshot,
            "post_cleanup_snapshot": self.post_cleanup_snapshot,
            "fatal": self.fatal,
        }


def verify_evidence(
    directory: Path, *, run_id: str, world_id: str
) -> SimJournalEvidence:
    root = directory.resolve()
    if not root.is_dir():
        raise ContainerInfrastructureError("sim evidence directory is missing")
    names = {path.name for path in root.iterdir()}
    if "fatal.json" in names:
        if not names.issubset({"fatal.json", "events.jsonl"}):
            raise ContainerInfrastructureError("unexpected sim fatal evidence files")
        fatal = _load_json(root / "fatal.json", FATAL_LIMIT, "fatal")
        required = {
            "schema_version",
            "run_id",
            "failure_kind",
            "exception_type",
            "message",
        }
        if set(fatal) != required and set(fatal) != required | {"final_hash"}:
            raise ContainerInfrastructureError("invalid sim fatal marker")
        if (
            fatal["schema_version"] != 1
            or isinstance(fatal["schema_version"], bool)
            or fatal["run_id"] != run_id
            or fatal["failure_kind"] != "trusted_sim_failure"
            or not isinstance(fatal["exception_type"], str)
            or not fatal["exception_type"]
            or not isinstance(fatal["message"], str)
            or not fatal["message"]
        ):
            raise ContainerInfrastructureError("sim fatal marker mismatch")
        events = (
            _load_events(root / "events.jsonl", run_id, world_id)
            if "events.jsonl" in names
            else ()
        )
        if events and (
            events[-1]["kind"] != "trusted.fatal"
            or events[-1]["fields"]
            != {key: value for key, value in fatal.items() if key != "final_hash"}
        ):
            raise ContainerInfrastructureError("fatal event does not match marker")
        final_hash = events[-1]["event_hash"] if events else None
        if (events and "final_hash" not in fatal) or (
            not events and "final_hash" in fatal
        ):
            raise ContainerInfrastructureError("fatal terminal hash is missing")
        if "final_hash" in fatal and (
            not isinstance(fatal["final_hash"], str)
            or not HASH.fullmatch(fatal["final_hash"])
            or fatal["final_hash"] != final_hash
        ):
            raise ContainerInfrastructureError("fatal terminal hash mismatch")
        return SimJournalEvidence(
            events=events,
            event_count=len(events),
            final_hash=final_hash,
            pre_cleanup_snapshot=None,
            post_cleanup_snapshot=None,
            fatal=fatal,
        )
    if names != {"events.jsonl", "summary.json"}:
        raise ContainerInfrastructureError("sim evidence files are incomplete")
    events = _load_events(root / "events.jsonl", run_id, world_id)
    summary = _load_json(root / "summary.json", SUMMARY_LIMIT, "summary")
    expected_keys = {
        "schema_version",
        "run_id",
        "world_id",
        "broker",
        "pre_cleanup_snapshot",
        "post_cleanup_snapshot",
        "event_count",
        "final_hash",
    }
    if set(summary) != expected_keys:
        raise ContainerInfrastructureError("invalid sim summary fields")
    final_hash = events[-1]["event_hash"] if events else "0" * 64
    if (
        summary["schema_version"] != 1
        or isinstance(summary["schema_version"], bool)
        or summary["run_id"] != run_id
        or summary["world_id"] != world_id
        or not isinstance(summary["event_count"], int)
        or isinstance(summary["event_count"], bool)
        or summary["event_count"] != len(events)
        or not isinstance(summary["final_hash"], str)
        or not HASH.fullmatch(summary["final_hash"])
        or summary["final_hash"] != final_hash
    ):
        raise ContainerInfrastructureError("sim summary does not match journal")
    broker = summary["broker"]
    if (
        not isinstance(broker, dict)
        or set(broker) != {"connections", "leaked_sessions", "frozen"}
        or not all(
            isinstance(broker[key], int)
            and not isinstance(broker[key], bool)
            and broker[key] >= 0
            for key in ("connections", "leaked_sessions")
        )
        or broker["frozen"] is not True
    ):
        raise ContainerInfrastructureError("invalid sim broker summary")
    if (
        not events
        or events[0]["kind"] != "lifecycle.start"
        or events[-1]["kind"] != "lifecycle.finalized"
    ):
        raise ContainerInfrastructureError("sim lifecycle events are incomplete")
    kinds = [event["kind"] for event in events]
    if (
        kinds.count("broker.ready") != 1
        or kinds.count("broker.frozen") != 1
        or kinds.count("state.force_safe") != 1
    ):
        raise ContainerInfrastructureError("sim broker lifecycle events are incomplete")
    ready_index = kinds.index("broker.ready")
    frozen_index = kinds.index("broker.frozen")
    safe_index = kinds.index("state.force_safe")
    if not 0 < ready_index < frozen_index < safe_index < len(events) - 1:
        raise ContainerInfrastructureError("sim broker lifecycle ordering is invalid")
    frozen = events[frozen_index]["fields"]
    if frozen != {
        "connections": broker["connections"],
        "leaked_sessions": broker["leaked_sessions"],
    }:
        raise ContainerInfrastructureError("sim frozen event does not match summary")
    safe_state = events[safe_index]["fields"].get("state_after")
    psu = safe_state.get("psu") if isinstance(safe_state, dict) else None
    awg = safe_state.get("awg") if isinstance(safe_state, dict) else None
    switch = safe_state.get("switch") if isinstance(safe_state, dict) else None
    if (
        not isinstance(safe_state, dict)
        or not isinstance(psu, dict)
        or psu.get("output") is not False
        or not isinstance(awg, dict)
        or awg.get("output") is not False
        or not isinstance(switch, dict)
        or switch.get("closed_routes") != []
    ):
        raise ContainerInfrastructureError("sim force-safe event is unsafe")
    before = summary["pre_cleanup_snapshot"]
    after = summary["post_cleanup_snapshot"]
    if not _valid_snapshot(before) or not _valid_snapshot(after):
        raise ContainerInfrastructureError("sim cleanup snapshots are invalid")
    terminal = events[-1]["fields"]
    if (
        terminal.get("pre_cleanup_snapshot") != before
        or terminal.get("post_cleanup_snapshot") != after
        or terminal.get("broker") != summary["broker"]
    ):
        raise ContainerInfrastructureError("sim terminal event does not match summary")
    if after.get("safe") is not True:
        raise ContainerInfrastructureError("sim post-cleanup state is unsafe")
    return SimJournalEvidence(
        events=events,
        event_count=len(events),
        final_hash=final_hash,
        pre_cleanup_snapshot=before,
        post_cleanup_snapshot=after,
        fatal=None,
    )


def _load_events(path: Path, run_id: str, world_id: str) -> tuple[dict[str, Any], ...]:
    payload = _read_regular(path, EVENTS_LIMIT, "events")
    records: list[dict[str, Any]] = []
    previous = "0" * 64
    previous_time = -1
    for sequence, raw in enumerate(payload.splitlines(), 1):
        if not raw:
            raise ContainerInfrastructureError("blank sim journal line")
        try:
            value = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ContainerInfrastructureError("malformed sim journal JSON") from exc
        expected = {
            "run_id",
            "world_id",
            "sequence",
            "monotonic_ns",
            "previous_hash",
            "kind",
            "fields",
            "event_hash",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ContainerInfrastructureError("invalid sim journal event")
        if (
            value["run_id"] != run_id
            or value["world_id"] != world_id
            or value["sequence"] != sequence
            or isinstance(value["sequence"], bool)
            or not isinstance(value["monotonic_ns"], int)
            or isinstance(value["monotonic_ns"], bool)
            or value["monotonic_ns"] < previous_time
            or value["previous_hash"] != previous
            or not isinstance(value["kind"], str)
            or not value["kind"]
            or not isinstance(value["fields"], dict)
            or not isinstance(value["event_hash"], str)
            or not HASH.fullmatch(value["event_hash"])
        ):
            raise ContainerInfrastructureError("sim journal sequence or fields invalid")
        unsigned = {key: item for key, item in value.items() if key != "event_hash"}
        digest = hashlib.sha256(_canonical(unsigned)).hexdigest()
        if digest != value["event_hash"]:
            raise ContainerInfrastructureError("sim journal hash mismatch")
        previous = digest
        previous_time = value["monotonic_ns"]
        records.append(value)
    return tuple(records)


def _load_json(path: Path, limit: int, label: str) -> dict[str, Any]:
    payload = _read_regular(path, limit, label)
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContainerInfrastructureError(f"malformed sim {label} JSON") from exc
    if not isinstance(value, dict):
        raise ContainerInfrastructureError(f"sim {label} must be an object")
    return value


def _read_regular(path: Path, limit: int, label: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ContainerInfrastructureError(f"invalid sim {label} file")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > limit:
                raise ContainerInfrastructureError(f"oversized sim {label} file")
            return payload
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ContainerInfrastructureError(f"cannot read sim {label} file") from exc


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _valid_snapshot(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "clock_ms",
        "closed_routes",
        "psu_voltage_v",
        "psu_output",
        "awg_waveform_name",
        "awg_points",
        "awg_amplitude_vpp",
        "awg_output",
        "stimulus_started_ms",
        "safe",
    }:
        return False
    integer_fields = ("clock_ms",)
    if any(
        not isinstance(value[key], int)
        or isinstance(value[key], bool)
        or value[key] < 0
        for key in integer_fields
    ):
        return False
    stimulus = value["stimulus_started_ms"]
    if stimulus is not None and (
        not isinstance(stimulus, int)
        or isinstance(stimulus, bool)
        or stimulus < 0
    ):
        return False
    if not all(
        isinstance(value[key], bool)
        for key in ("psu_output", "awg_output", "safe")
    ):
        return False
    waveform = value["awg_waveform_name"]
    if waveform is not None and not isinstance(waveform, str):
        return False
    routes = value["closed_routes"]
    if not isinstance(routes, list) or not all(
        isinstance(route, str) and route for route in routes
    ):
        return False
    points = value["awg_points"]
    if not isinstance(points, list) or not all(
        _finite_number(point) for point in points
    ):
        return False
    return all(
        _finite_number(value[key])
        for key in ("psu_voltage_v", "awg_amplitude_vpp")
    )


def _finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
