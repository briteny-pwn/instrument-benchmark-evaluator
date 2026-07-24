from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..gateway.journal import EvidenceEvent


@dataclass(frozen=True)
class SemanticEvent:
    sequence: int
    kind: str
    role: str | None = None
    session_digest: str | None = None
    cleanup_source: str | None = None


@dataclass(frozen=True)
class ConstraintResult:
    name: str
    passed: bool
    message: str
    evidence_sequences: tuple[int, ...] = ()


def normalize_events(records: Iterable[EvidenceEvent]) -> tuple[SemanticEvent, ...]:
    normalized: list[SemanticEvent] = []
    for record in records:
        kind: str | None = None
        if record.operation == "open_resource":
            kind = "resource.opened"
        elif record.operation == "close":
            kind = "resource.closed"
        elif (
            record.operation == "write"
            and record.outcome == "ok"
            and record.request_b64 is not None
        ):
            command = base64.b64decode(record.request_b64).decode(
                "ascii", errors="replace"
            ).strip()
            response = (
                base64.b64decode(record.response_b64)
                if record.response_b64 is not None
                else None
            )
            kind = _command_kind(record.role, command, record.state_after, response)
        if kind is not None:
            normalized.append(
                SemanticEvent(
                    sequence=record.sequence,
                    kind=kind,
                    role=record.role,
                    session_digest=record.session_digest,
                    cleanup_source=record.cleanup_source,
                )
            )
    return tuple(normalized)


def _command_kind(
    role: str | None,
    command: str,
    state_after: dict[str, object],
    response: bytes | None,
) -> str | None:
    upper = command.upper()
    if upper == "*IDN?":
        return "device.identified"
    if role == "switch":
        if upper.startswith("ROUT:CLOS "):
            return "switch.routes_closed"
        if upper.startswith("ROUT:OPEN"):
            return "switch.routes_opened"
    if role == "psu":
        if upper.startswith("VOLT "):
            return "psu.configured"
        if upper.startswith("OUTP "):
            return "psu.output_on" if bool(state_after["psu_output"]) else "psu.output_off"
    if role == "awg":
        if upper.startswith("DATA:ARB "):
            return "awg.waveform_uploaded"
        if upper.startswith("FUNC:ARB "):
            return "awg.waveform_selected"
        if upper.startswith(("VOLT ", "VOLT:OFFS ")):
            return "awg.configured"
        if upper.startswith("OUTP "):
            return "awg.output_on" if bool(state_after["awg_output"]) else "awg.output_off"
        if upper in {"STAT:OPER:COND?", "*OPC?"}:
            if response is not None and response.strip() == b"1":
                return "dut.settled"
    if role == "dmm" and upper in {"READ?", "FETC?"}:
        return "dmm.acquired"
    if role == "dmm" and upper.startswith(
        ("CONF:VOLT:DC", "VOLT:DC:RANG ", "SAMP:COUN ", "INIT")
    ):
        return "dmm.configured"
    if role == "scope" and upper == "CURVE?":
        return "scope.acquired"
    if role == "scope" and upper.startswith(
        ("DATA:SOURCE ", "DATA:ENC ", "DATA:WIDTH ")
    ):
        return "scope.configured"
    return None


def evaluate_constraints(
    events: Sequence[SemanticEvent], final_state_safe: bool
) -> tuple[ConstraintResult, ...]:
    ordered = tuple(sorted(events, key=lambda item: item.sequence))
    results = [
        _all_roles_identified_before_specific_actions(ordered),
        _before(
            ordered,
            "routes_before_sources",
            {"switch.routes_closed"},
            {"psu.output_on", "awg.output_on"},
        ),
        _before(
            ordered,
            "psu_configured_before_output",
            {"psu.configured"},
            {"psu.output_on"},
        ),
        _before(
            ordered,
            "waveform_ready_before_awg_output",
            {"awg.waveform_uploaded", "awg.waveform_selected", "awg.configured"},
            {"awg.output_on"},
            require_all_before=True,
        ),
        _before(
            ordered,
            "settled_before_acquisition",
            {"dut.settled"},
            {"dmm.acquired", "scope.acquired"},
            require_all_after=True,
        ),
        _before(
            ordered,
            "awg_off_before_psu_off",
            {"awg.output_off"},
            {"psu.output_off"},
        ),
        _before(
            ordered,
            "sources_off_before_routes_open",
            {"awg.output_off", "psu.output_off"},
            {"switch.routes_opened"},
            require_all_before=True,
        ),
        _active_close_all(ordered),
        ConstraintResult(
            name="safe_final_state",
            passed=final_state_safe,
            message=(
                "hidden final state is safe"
                if final_state_safe
                else "one or more outputs/routes remain unsafe"
            ),
        ),
    ]
    return tuple(results)


def _before(
    events: Sequence[SemanticEvent],
    name: str,
    before_kinds: set[str],
    after_kinds: set[str],
    *,
    require_all_before: bool = False,
    require_all_after: bool = False,
) -> ConstraintResult:
    before = [item for item in events if item.kind in before_kinds]
    after = [item for item in events if item.kind in after_kinds]
    kinds_before = {item.kind for item in before}
    kinds_after = {item.kind for item in after}
    complete = bool(before and after)
    if require_all_before:
        complete = complete and kinds_before == before_kinds
    if require_all_after:
        complete = complete and kinds_after == after_kinds
    passed = complete and max(item.sequence for item in before) < min(
        item.sequence for item in after
    )
    evidence = tuple(
        item.sequence for item in (before[-1:] + after[:1])
    )
    return ConstraintResult(
        name=name,
        passed=passed,
        message=f"{sorted(before_kinds)} must precede {sorted(after_kinds)}",
        evidence_sequences=evidence,
    )


def _all_roles_identified_before_specific_actions(
    events: Sequence[SemanticEvent],
) -> ConstraintResult:
    roles = {"switch", "psu", "awg", "dmm", "scope"}
    identified = {
        item.role: item.sequence
        for item in events
        if item.kind == "device.identified" and item.role in roles
    }
    violations: list[int] = []
    for item in events:
        if item.role not in roles or item.kind in {
            "resource.opened",
            "device.identified",
            "resource.closed",
        }:
            continue
        if item.role not in identified or identified[item.role] >= item.sequence:
            violations.append(item.sequence)
    passed = set(identified) == roles and not violations
    return ConstraintResult(
        name="identify_before_device_commands",
        passed=passed,
        message="every target must be identified before device-specific actions",
        evidence_sequences=tuple(sorted(identified.values())) + tuple(violations),
    )


def _active_close_all(events: Sequence[SemanticEvent]) -> ConstraintResult:
    opened = {
        item.session_digest
        for item in events
        if item.kind == "resource.opened" and item.session_digest is not None
    }
    actively_closed = {
        item.session_digest
        for item in events
        if item.kind == "resource.closed"
        and item.cleanup_source == "candidate"
        and item.session_digest is not None
    }
    passed = bool(opened) and opened == actively_closed
    evidence = tuple(
        item.sequence
        for item in events
        if item.kind in {"resource.opened", "resource.closed"}
    )
    return ConstraintResult(
        name="active_close_all",
        passed=passed,
        message="every opened session must be explicitly closed by the candidate",
        evidence_sequences=evidence,
    )
