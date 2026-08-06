from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from pyvisa.constants import ResourceAttribute

from sources.pyvisa.pyvisa_dut_validation_v1.gateway.journal import EvidenceEvent
from sources.pyvisa.pyvisa_dut_validation_v1.models import WorldSnapshot

from .journal import JournalEvent


TIMEOUT_ATTRIBUTE = int(ResourceAttribute.timeout_value)
TERMCHAR_ATTRIBUTE = int(ResourceAttribute.termchar)
TERMCHAR_ENABLED_ATTRIBUTE = int(ResourceAttribute.termchar_enabled)


class ProjectionError(ValueError):
    """The verified raw journal cannot be projected consistently."""


@dataclass
class _Session:
    digest: str
    connection_id: str
    resource: str | None = None
    role: str | None = None
    closed: bool = False
    termchar_seen: bool = False
    termchar_enabled: bool = False


@dataclass(frozen=True)
class _Draft:
    raw: JournalEvent
    operation: str
    session_digest: str | None
    request: bytes | None
    response: bytes | None
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    role: str | None = None
    resource: str | None = None
    outcome: str = "ok"
    error_code: str | None = None
    cleanup_source: str | None = None
    offset: int = 0


def project_events(raw: Sequence[JournalEvent]) -> tuple[EvidenceEvent, ...]:
    records = tuple(raw)
    if not records:
        return ()
    _validate_records(records)
    sessions: dict[str, _Session] = {}
    requests: dict[str, JournalEvent] = {}
    writes: dict[str, tuple[JournalEvent, bytes]] = {}
    inferred_write_termination: set[str] = set()
    inferred_read_termination: set[str] = set()
    drafts: list[_Draft] = []
    last_state = _initial_state(records)
    active_hook_digest: str | None = None
    start_ns = records[0].monotonic_ns
    stimulus_started_ms: int | None = None

    for event in records:
        fields = event.fields
        clock_ms = max(0, (event.monotonic_ns - start_ns) // 1_000_000)
        current_state = _clocked(last_state, clock_ms, stimulus_started_ms)
        if event.kind == "rpc.request":
            connection_id = _text(fields, "connection_id")
            requests[connection_id] = event
            if fields.get("operation") == "write":
                args = _mapping(fields, "args")
                digest = _optional_token_digest(args.get("session"))
                if digest is not None:
                    try:
                        payload = _audited_bytes(args.get("data"))
                    except ProjectionError:
                        payload = None
                    if payload is not None:
                        writes[digest] = (event, payload)
            continue

        if event.kind in {"hook.before", "hook.after", "hook.error"}:
            digest = (
                _text(fields, "session_digest")
                if event.kind == "hook.before"
                else active_hook_digest
            )
            if digest is None:
                raise ProjectionError("hook completion has no matching hook start")
            session = sessions.get(digest)
            if session is not None:
                session.resource = _text(fields, "resource")
                session.role = _text(fields, "role")
            if event.kind == "hook.before":
                if active_hook_digest is not None:
                    raise ProjectionError("nested hook command is invalid")
                active_hook_digest = digest
                last_state = _snapshot(
                    _mapping(fields, "state_before"),
                    clock_ms=clock_ms,
                    stimulus_started_ms=stimulus_started_ms,
                )
                continue
            before = _snapshot(
                _mapping(fields, "state_before"),
                clock_ms=clock_ms,
                stimulus_started_ms=stimulus_started_ms,
            )
            after_raw = _mapping(fields, "state_after")
            after_output = _nested_awg_output(after_raw)
            if not before["awg_output"] and after_output:
                stimulus_started_ms = clock_ms
            elif not after_output:
                stimulus_started_ms = None
            after = _snapshot(
                after_raw,
                clock_ms=clock_ms,
                stimulus_started_ms=stimulus_started_ms,
            )
            pending = writes.pop(digest, None)
            if pending is None:
                raise ProjectionError("hook completion has no raw write request")
            write_event, request = pending
            if request.endswith(b"\n") and digest not in inferred_write_termination:
                drafts.append(
                    _Draft(
                        raw=write_event,
                        operation="set_write_termination",
                        session_digest=digest,
                        request=b"\n",
                        response=None,
                        state_before=before,
                        state_after=before,
                        role=_optional_text(fields.get("role")),
                        resource=_optional_text(fields.get("resource")),
                        offset=1,
                    )
                )
                inferred_write_termination.add(digest)
            response = (
                _hook_response(fields.get("response"))
                if event.kind == "hook.after"
                else None
            )
            drafts.append(
                _Draft(
                    raw=event,
                    operation="write",
                    session_digest=digest,
                    request=request,
                    response=response,
                    state_before=before,
                    state_after=after,
                    role=_optional_text(fields.get("role")),
                    resource=_optional_text(fields.get("resource")),
                    outcome="ok" if event.kind == "hook.after" else "error",
                    error_code=(
                        None
                        if event.kind == "hook.after"
                        else str(fields.get("error_code", "instrument_error"))
                    ),
                )
            )
            last_state = after
            active_hook_digest = None
            continue

        if event.kind == "rpc.result":
            connection_id = _text(fields, "connection_id")
            request = requests.pop(connection_id, None)
            if request is None:
                raise ProjectionError("RPC result has no request")
            operation = _text(fields, "operation")
            if request.fields.get("operation") != operation:
                raise ProjectionError("RPC result operation mismatch")
            args = _mapping(request.fields, "args")
            if operation == "open_default_resource_manager":
                token = _text(fields, "result")
                digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
                sessions[digest] = _Session(digest, connection_id)
            elif operation == "open":
                token = _text(fields, "result")
                digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
                session = _Session(
                    digest=digest,
                    connection_id=connection_id,
                    resource=_text(args, "resource_name"),
                )
                sessions[digest] = session
                drafts.append(
                    _Draft(
                        raw=event,
                        operation="open_resource",
                        session_digest=digest,
                        request=session.resource.encode("utf-8"),
                        response=None,
                        state_before=current_state,
                        state_after=current_state,
                    )
                )
            elif operation == "set_attribute":
                digest = _token_digest(args.get("session"))
                session = _required_session(sessions, digest)
                attribute = _integer(args, "attribute")
                projected_operation: str | None = None
                if attribute == TIMEOUT_ATTRIBUTE:
                    projected_operation = "set_timeout"
                elif attribute == TERMCHAR_ATTRIBUTE:
                    session.termchar_seen = args.get("attribute_state") == 10
                elif attribute == TERMCHAR_ENABLED_ATTRIBUTE:
                    session.termchar_enabled = args.get("attribute_state") is True
                if (
                    digest not in inferred_read_termination
                    and session.termchar_seen
                    and session.termchar_enabled
                ):
                    projected_operation = "set_read_termination"
                    inferred_read_termination.add(digest)
                if projected_operation is not None:
                    request_payload = (
                        b"\n"
                        if projected_operation == "set_read_termination"
                        else str(args.get("attribute_state")).encode("ascii")
                    )
                    drafts.append(
                        _Draft(
                            raw=event,
                            operation=projected_operation,
                            session_digest=digest,
                            request=request_payload,
                            response=None,
                            state_before=current_state,
                            state_after=current_state,
                        )
                    )
            elif operation == "close":
                digest = _token_digest(args.get("session"))
                session = _required_session(sessions, digest)
                if session.resource is not None and not session.closed:
                    drafts.append(
                        _Draft(
                            raw=event,
                            operation="close",
                            session_digest=digest,
                            request=None,
                            response=None,
                            state_before=current_state,
                            state_after=current_state,
                            cleanup_source="candidate",
                        )
                    )
                session.closed = True
            continue

        if event.kind == "rpc.reject":
            connection_id = _text(fields, "connection_id")
            request = requests.pop(connection_id, event)
            args = request.fields.get("args", {})
            digest = (
                _optional_token_digest(args.get("session"))
                if isinstance(args, dict) and "session" in args
                else None
            )
            writes.pop(digest, None)
            error_kind = str(fields.get("error_kind", "rejected"))
            if (
                error_kind == "visa_error"
                and request.fields.get("operation") == "write"
            ):
                continue
            drafts.append(
                _Draft(
                    raw=event,
                    operation=(
                        "visa_error"
                        if error_kind == "visa_error"
                        else "rpc_reject"
                    ),
                    session_digest=digest,
                    request=None,
                    response=None,
                    state_before=current_state,
                    state_after=current_state,
                    outcome="error",
                    error_code=(
                        f"{error_kind}:"
                        f"{fields.get('code', fields.get('error_kind'))}"
                    ),
                )
            )
            continue

        if event.kind == "connection.reject":
            drafts.append(
                _Draft(
                    raw=event,
                    operation="protocol_reject",
                    session_digest=None,
                    request=None,
                    response=None,
                    state_before=current_state,
                    state_after=current_state,
                    outcome="error",
                    error_code=str(fields.get("reason", "malformed_frame")),
                )
            )
            continue

        if event.kind == "connection.close":
            connection_id = _text(fields, "connection_id")
            offset = 0
            projected_keys: set[str] = set()
            for session in sessions.values():
                if (
                    session.connection_id == connection_id
                    and session.resource is not None
                    and not session.closed
                ):
                    key = session.role or session.resource
                    session.closed = True
                    if key in projected_keys:
                        continue
                    projected_keys.add(key)
                    drafts.append(
                        _Draft(
                            raw=event,
                            operation="close",
                            session_digest=session.digest,
                            request=None,
                            response=None,
                            state_before=current_state,
                            state_after=current_state,
                            cleanup_source="forced",
                            offset=offset,
                        )
                    )
                    offset += 1
            continue

        if event.kind == "state.force_safe":
            before = _snapshot(
                _mapping(fields, "state_before"),
                clock_ms=clock_ms,
                stimulus_started_ms=stimulus_started_ms,
            )
            stimulus_started_ms = None
            after = _snapshot(
                _mapping(fields, "state_after"),
                clock_ms=clock_ms,
                stimulus_started_ms=None,
            )
            drafts.append(
                _Draft(
                    raw=event,
                    operation="force_safe",
                    session_digest=None,
                    request=None,
                    response=None,
                    state_before=before,
                    state_after=after,
                    cleanup_source="forced",
                )
            )
            last_state = after

    resource_roles = {
        session.resource: session.role
        for session in sessions.values()
        if session.resource is not None and session.role is not None
    }
    for session in sessions.values():
        if session.role is None and session.resource in resource_roles:
            session.role = resource_roles[session.resource]
    projected = tuple(
        sorted(
            (_materialize(draft, sessions) for draft in drafts),
            key=lambda event: event.sequence,
        )
    )
    if len({event.sequence for event in projected}) != len(projected):
        raise ProjectionError("projected event sequence collision")
    if active_hook_digest is not None or writes or requests:
        raise ProjectionError("raw journal contains incomplete operations")
    return projected


def _materialize(
    draft: _Draft, sessions: dict[str, _Session]
) -> EvidenceEvent:
    session = (
        sessions.get(draft.session_digest)
        if draft.session_digest is not None
        else None
    )
    request = draft.request or b""
    response = draft.response or b""
    before_hash = _digest_state(draft.state_before)
    after_hash = _digest_state(draft.state_after)
    return EvidenceEvent(
        run_id=draft.raw.run_id,
        world_id=draft.raw.world_id,
        sequence=draft.raw.sequence * 10 + draft.offset,
        monotonic_ns=draft.raw.monotonic_ns,
        operation=draft.operation,
        session_digest=draft.session_digest,
        resource=draft.resource if draft.resource is not None else (
            session.resource if session is not None else None
        ),
        role=draft.role if draft.role is not None else (
            session.role if session is not None else None
        ),
        request_b64=(
            base64.b64encode(draft.request).decode("ascii")
            if draft.request is not None
            else None
        ),
        response_b64=(
            base64.b64encode(draft.response).decode("ascii")
            if draft.response is not None
            else None
        ),
        request_sha256=hashlib.sha256(request).hexdigest(),
        response_sha256=hashlib.sha256(response).hexdigest(),
        request_bytes=len(request),
        response_bytes=len(response),
        state_before=draft.state_before,
        state_after=draft.state_after,
        state_before_sha256=before_hash,
        state_after_sha256=after_hash,
        outcome=draft.outcome,
        error_code=draft.error_code,
        cleanup_source=draft.cleanup_source,
        source_sequence=draft.raw.sequence,
    )


def _snapshot(
    value: dict[str, Any],
    *,
    clock_ms: int,
    stimulus_started_ms: int | None,
) -> dict[str, Any]:
    try:
        psu = _mapping(value, "psu")
        switch = _mapping(value, "switch")
        awg = _mapping(value, "awg")
        selected_raw = awg.get("selected")
        selected = str(selected_raw).upper() if selected_raw else None
        waveforms = awg.get("waveforms", {})
        if not isinstance(waveforms, dict):
            raise ProjectionError("invalid AWG waveform state")
        points_raw = waveforms.get(selected or "", [])
        if not isinstance(points_raw, list):
            raise ProjectionError("invalid AWG points state")
        routes = switch.get("closed_routes")
        if not isinstance(routes, list) or not all(
            isinstance(route, str) for route in routes
        ):
            raise ProjectionError("invalid switch route state")
        points = tuple(_finite_float(item) for item in points_raw)
        voltage = _finite_float(psu.get("voltage"))
        amplitude = _finite_float(awg.get("amplitude"))
        psu_output = _boolean(psu.get("output"))
        awg_output = _boolean(awg.get("output"))
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ProjectionError):
            raise
        raise ProjectionError("invalid nested simulator state") from exc
    snapshot = WorldSnapshot(
        clock_ms=clock_ms,
        closed_routes=tuple(sorted(routes)),
        psu_voltage_v=voltage,
        psu_output=psu_output,
        awg_waveform_name=selected,
        awg_points=points,
        awg_amplitude_vpp=amplitude,
        awg_output=awg_output,
        stimulus_started_ms=stimulus_started_ms,
        safe=not psu_output and not awg_output and not routes,
    )
    return asdict(snapshot)


def _initial_state(records: tuple[JournalEvent, ...]) -> dict[str, Any]:
    for event in records:
        if event.kind in {"hook.before", "state.force_safe"}:
            before = event.fields.get("state_before")
            if isinstance(before, dict):
                return _snapshot(
                    before, clock_ms=0, stimulus_started_ms=None
                )
    raise ProjectionError("raw journal has no trusted simulator state")


def _clocked(
    state: dict[str, Any], clock_ms: int, stimulus_started_ms: int | None
) -> dict[str, Any]:
    value = dict(state)
    value["clock_ms"] = clock_ms
    value["stimulus_started_ms"] = stimulus_started_ms
    return value


def _nested_awg_output(state: dict[str, Any]) -> bool:
    return _boolean(_mapping(state, "awg").get("output"))


def _digest_state(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_records(records: tuple[JournalEvent, ...]) -> None:
    run_id = records[0].run_id
    world_id = records[0].world_id
    previous_sequence = 0
    for event in records:
        if event.run_id != run_id or event.world_id != world_id:
            raise ProjectionError("raw journal identity mismatch")
        if event.sequence != previous_sequence + 1:
            raise ProjectionError("raw journal order is invalid")
        previous_sequence = event.sequence


def _required_session(sessions: dict[str, _Session], digest: str) -> _Session:
    try:
        return sessions[digest]
    except KeyError as exc:
        raise ProjectionError("RPC refers to an unknown session") from exc


def _token_digest(value: object) -> str:
    if (
        not isinstance(value, dict)
        or value.get("type") != "token"
        or not isinstance(value.get("sha256"), str)
    ):
        raise ProjectionError("invalid audited session token")
    return value["sha256"]


def _optional_token_digest(value: object) -> str | None:
    try:
        return _token_digest(value)
    except ProjectionError:
        return None


def _audited_bytes(value: object) -> bytes:
    if not isinstance(value, dict) or value.get("type") != "bytes":
        raise ProjectionError("invalid audited bytes")
    try:
        payload = base64.b64decode(value["base64"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectionError("invalid audited byte encoding") from exc
    if (
        value.get("length") != len(payload)
        or value.get("sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise ProjectionError("audited byte digest mismatch")
    return payload


def _hook_response(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProjectionError("invalid hook response")
    try:
        payload = base64.b64decode(value["base64"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectionError("invalid hook response encoding") from exc
    if value.get("length") != len(payload):
        raise ProjectionError("hook response length mismatch")
    return payload


def _mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ProjectionError(f"raw event {key} must be an object")
    return result


def _text(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ProjectionError(f"raw event {key} must be text")
    return result


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: dict[str, Any], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ProjectionError(f"raw event {key} must be an integer")
    return result


def _finite_float(value: object) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProjectionError("simulator state number is invalid") from exc
    if not math.isfinite(converted):
        raise ProjectionError("simulator state number is not finite")
    return converted


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.upper() in {"ON", "OFF"}:
        return value.upper() == "ON"
    raise ProjectionError("simulator output state is invalid")
