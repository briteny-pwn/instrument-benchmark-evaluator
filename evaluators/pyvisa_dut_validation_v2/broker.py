from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyvisa import errors
from pyvisa.constants import (
    AccessModes,
    EventMechanism,
    EventType,
    ResourceAttribute,
    StatusCode,
)
from pyvisa_sim.hooks import CommandRejected

from .journal import EventJournal
from .protocol import (
    ConnectionClosed,
    ProtocolError,
    WireValue,
    decode_request,
    encode_message,
    error_response,
    recv_message,
    success_response,
)
from .query_filter import ResourceQueryRejected, filter_resources


OPERATIONS = {
    "open_default_resource_manager",
    "list_resources",
    "open",
    "close",
    "read",
    "write",
    "get_attribute",
    "set_attribute",
    "disable_event",
    "discard_events",
}
ERROR_INVALID_OBJECT = int(StatusCode.error_invalid_object)
ERROR_NONSUPPORTED = int(StatusCode.error_nonsupported_operation)


@dataclass
class RemoteSession:
    real_session: int
    session_kind: str


@dataclass
class ConnectionState:
    connection_id: str
    peer_uid: int
    peer_gid: int
    peer_pid: int
    sessions: dict[str, RemoteSession] = field(default_factory=dict)
    request_count: int = 0
    closed: bool = False
    connection: socket.socket | None = field(default=None, repr=False)


@dataclass(frozen=True)
class BrokerSummary:
    connections: int
    leaked_sessions: int
    frozen: bool


class CandidateRequestError(ValueError):
    def __init__(self, kind: str, code: int, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message


class RemoteVisaBroker:
    def __init__(
        self,
        bench: Any,
        journal: EventJournal,
        *,
        max_connections: int = 16,
        max_total_connections: int = 64,
        max_requests_per_connection: int = 256,
        max_total_requests: int = 256,
        max_total_argument_bytes: int = 512 * 1024,
    ) -> None:
        limits = (
            max_connections,
            max_total_connections,
            max_requests_per_connection,
            max_total_requests,
            max_total_argument_bytes,
        )
        if any(
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            for limit in limits
        ):
            raise ValueError("broker limits must be positive integers")
        self.bench = bench
        self.journal = journal
        self.max_connections = max_connections
        self.max_total_connections = max_total_connections
        self.max_requests_per_connection = max_requests_per_connection
        self.max_total_requests = max_total_requests
        self.max_total_argument_bytes = max_total_argument_bytes
        self._states: list[ConnectionState] = []
        self._workers: list[threading.Thread] = []
        self._connection_count = 0
        self._request_count = 0
        self._argument_bytes = 0
        self._leaked_session_count = 0
        self._state_lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._ready = threading.Event()
        self._frozen = False
        self._summary: BrokerSummary | None = None
        self._fatal_error: BaseException | None = None

    def new_connection(
        self, uid: int, gid: int = 0, pid: int = 0
    ) -> ConnectionState:
        state = ConnectionState(secrets.token_hex(16), uid, gid, pid)
        with self._state_lock:
            if self._frozen:
                raise RuntimeError("broker is frozen")
            if (
                self._connection_count >= self.max_total_connections
                or len(self._states) >= self.max_connections
            ):
                self.journal.append(
                    "connection.reject",
                    reason="connection_limit",
                    peer_uid=uid,
                    peer_gid=gid,
                    peer_pid=pid,
                )
                raise CandidateRequestError(
                    "connection_limit",
                    ERROR_NONSUPPORTED,
                    "connection limit exceeded",
                )
            self._states.append(state)
            self._connection_count += 1
        self.journal.append(
            "connection.open",
            connection_id=state.connection_id,
            peer_uid=uid,
            peer_gid=gid,
            peer_pid=pid,
        )
        return state

    def dispatch(
        self,
        state: ConnectionState,
        operation: str,
        args: dict[str, WireValue],
    ) -> tuple[WireValue, int | None]:
        started_ns = time.monotonic_ns()
        unredacted_args = _audit_mapping(args)
        argument_bytes = len(operation.encode("utf-8")) + len(
            json.dumps(
                unredacted_args,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        )
        with self._state_lock:
            state.request_count += 1
            self._request_count += 1
            self._argument_bytes += argument_bytes
            request_limit_exceeded = (
                state.request_count > self.max_requests_per_connection
                or self._request_count > self.max_total_requests
                or self._argument_bytes > self.max_total_argument_bytes
            )
        audited_operation = _audit_operation(
            operation, redact=request_limit_exceeded
        )
        self.journal.append(
            "rpc.request",
            connection_id=state.connection_id,
            operation=audited_operation,
            args=(
                _audit_rejected_mapping(args)
                if request_limit_exceeded
                else unredacted_args
            ),
        )
        try:
            if request_limit_exceeded:
                raise CandidateRequestError(
                    "request_limit",
                    ERROR_NONSUPPORTED,
                    "request limit exceeded",
                )
            if state.closed:
                raise CandidateRequestError(
                    "closed_connection", ERROR_INVALID_OBJECT, "connection is closed"
                )
            if operation == "hello":
                self._exact(args, set())
                if state.peer_uid not in {10001, 11001}:
                    raise CandidateRequestError(
                        "unauthorized", ERROR_INVALID_OBJECT, "unauthorized peer"
                    )
                result: WireValue = tuple(sorted(OPERATIONS))
                status = None
            else:
                if state.peer_uid != 10001:
                    raise CandidateRequestError(
                        "unauthorized", ERROR_INVALID_OBJECT, "unauthorized peer"
                    )
                if operation not in OPERATIONS:
                    raise CandidateRequestError(
                        "unknown_operation",
                        ERROR_NONSUPPORTED,
                        "unsupported operation",
                    )
                with self._operation_lock:
                    result, status = self._invoke(state, operation, args)
            self.journal.append(
                "rpc.result",
                connection_id=state.connection_id,
                operation=operation,
                result=_audit_value(result),
                status=status,
                latency_ns=max(0, time.monotonic_ns() - started_ns),
            )
            return result, status
        except CandidateRequestError as exc:
            self._record_reject(state, audited_operation, exc, started_ns)
            raise
        except errors.VisaIOError as exc:
            rejection = CandidateRequestError(
                "visa_error", int(exc.error_code), "VISA operation failed"
            )
            self._record_reject(
                state, audited_operation, rejection, started_ns
            )
            raise rejection from None
        except CommandRejected as exc:
            rejection = CandidateRequestError(
                "visa_error", exc.code, "VISA operation failed"
            )
            self._record_reject(
                state, audited_operation, rejection, started_ns
            )
            raise rejection from None
        except ResourceQueryRejected as exc:
            rejection = CandidateRequestError(
                "invalid_resource_query", exc.code, "VISA resource query rejected"
            )
            self._record_reject(
                state, audited_operation, rejection, started_ns
            )
            raise rejection from None
        except (TypeError, ValueError, KeyError) as exc:
            rejection = CandidateRequestError(
                "invalid_arguments", ERROR_INVALID_OBJECT, "invalid arguments"
            )
            self._record_reject(
                state, audited_operation, rejection, started_ns
            )
            raise rejection from exc
        except BaseException as exc:
            self._record_trusted_failure("broker", exc)
            raise

    def serve_unix(
        self,
        endpoint: Path,
        stop_event: threading.Event,
        *,
        candidate_uid: int = 10001,
        readiness_uid: int = 11001,
    ) -> None:
        endpoint = Path(endpoint)
        endpoint.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(endpoint.parent, 0o755)
        if endpoint.exists() or endpoint.is_symlink():
            raise RuntimeError("VISA socket endpoint already exists")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(endpoint))
            os.chmod(endpoint, 0o666)
            listener.listen(16)
            self.journal.append(
                "lifecycle.socket_bound",
                endpoint_name=endpoint.name,
                mode="0666",
            )
            listener.settimeout(0.1)
            self._ready.set()
            self.journal.append("broker.ready", endpoint_name=endpoint.name)
            while not stop_event.is_set():
                with self._state_lock:
                    traffic_limit_reached = (
                        self._connection_count >= self.max_total_connections
                        or self._request_count >= self.max_total_requests
                        or self._argument_bytes >= self.max_total_argument_bytes
                    )
                if traffic_limit_reached:
                    stop_event.wait(0.1)
                    continue
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                uid, gid, pid = _peer_credentials(connection)
                effective_uid = 10001 if uid == candidate_uid else 11001 if uid == readiness_uid else uid
                try:
                    state = self.new_connection(effective_uid, gid, pid)
                except CandidateRequestError:
                    connection.close()
                    time.sleep(0.01)
                    continue
                worker = threading.Thread(
                    target=self._serve_connection,
                    args=(connection, state),
                    daemon=True,
                )
                with self._state_lock:
                    self._workers.append(worker)
                worker.start()
        finally:
            listener.close()
            with self._state_lock:
                workers = tuple(self._workers)
            try:
                endpoint.unlink()
            except FileNotFoundError:
                pass
            self._ready.clear()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def disconnect(self, state: ConnectionState) -> int:
        with self._operation_lock:
            if state.closed:
                return 0
            leaked = len(state.sessions)
            for token, remote in tuple(reversed(tuple(state.sessions.items()))):
                status = self.bench.visalib.close(remote.real_session)
                self.journal.append(
                    "session.forced_cleanup",
                    connection_id=state.connection_id,
                    session=_audit_token(token),
                    session_kind=remote.session_kind,
                    status=int(status),
                )
            state.sessions.clear()
            state.closed = True
            self._leaked_session_count += leaked
        self.journal.append(
            "connection.close",
            connection_id=state.connection_id,
            leaked_sessions=leaked,
        )
        with self._state_lock:
            if state in self._states:
                self._states.remove(state)
        return leaked

    def freeze_and_close(self) -> BrokerSummary:
        with self._state_lock:
            if self._summary is not None:
                return self._summary
            self._frozen = True
            states = tuple(self._states)
            workers = tuple(self._workers)
        self.bench.cancel_operations()
        self.journal.append(
            "broker.cancellation_requested",
            active_workers=len(workers),
            active_connections=len(states),
        )
        for state in states:
            connection = state.connection
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RD)
                except OSError:
                    pass
        for worker in workers:
            worker.join(2.0)
        alive = sum(worker.is_alive() for worker in workers)
        if alive:
            raise RuntimeError("broker workers did not stop during finalization")
        for state in states:
            self.disconnect(state)
        summary = BrokerSummary(
            self._connection_count, self._leaked_session_count, True
        )
        with self._state_lock:
            self._summary = summary
        self.journal.append(
            "broker.frozen",
            connections=summary.connections,
            leaked_sessions=summary.leaked_sessions,
        )
        return summary

    def raise_if_failed(self) -> None:
        with self._state_lock:
            failure = self._fatal_error
        if failure is not None:
            raise RuntimeError("trusted broker worker failed") from failure

    def _serve_connection(
        self, connection: socket.socket, state: ConnectionState
    ) -> None:
        state.connection = connection
        try:
            while True:
                try:
                    message = recv_message(connection)
                    request_id, operation, args = decode_request(message)
                    try:
                        result, status = self.dispatch(state, operation, args)
                        response = success_response(request_id, result, status)
                        close_after_response = False
                    except CandidateRequestError as exc:
                        response = error_response(
                            request_id, exc.kind, exc.code, exc.message
                        )
                        close_after_response = exc.kind == "request_limit"
                    connection.sendall(encode_message(response))
                    if close_after_response:
                        break
                except ConnectionClosed:
                    break
                except ProtocolError as exc:
                    self.journal.append(
                        "connection.reject",
                        connection_id=state.connection_id,
                        reason="malformed_frame",
                        detail=str(exc),
                    )
                    break
                except (BrokenPipeError, ConnectionResetError):
                    break
        except BaseException as exc:
            self._record_trusted_failure("broker_worker", exc)
        finally:
            connection.close()
            state.connection = None
            self.disconnect(state)
            current = threading.current_thread()
            with self._state_lock:
                if current in self._workers:
                    self._workers.remove(current)

    def _record_trusted_failure(
        self, component: str, error: BaseException
    ) -> None:
        with self._state_lock:
            if self._fatal_error is not None:
                return
            self._fatal_error = error
        self.journal.append(
            "trusted.failure_detected",
            component=component,
            exception_type=type(error).__name__,
            message="trusted simulator invariant failure",
        )

    def _invoke(
        self,
        state: ConnectionState,
        operation: Any,
        args: dict[str, WireValue],
    ) -> tuple[WireValue, int | None]:
        library = self.bench.visalib
        if operation == "open_default_resource_manager":
            self._exact(args, set())
            session, status = library.open_default_resource_manager()
            return self._token(
                state, int(session), session_kind="resource_manager"
            ), int(status)

        token, session = self._session(state, args)
        if operation == "list_resources":
            self._exact(args, {"session", "query"})
            query = _string(args["query"])
            resources = tuple(library.devices.list_resources())
            self.journal.append(
                "resource_query.request",
                connection_id=state.connection_id,
                query_length=len(query),
                query_sha256=hashlib.sha256(query.encode()).hexdigest(),
            )
            try:
                filtered = filter_resources(resources, query)
            except ResourceQueryRejected as exc:
                self.journal.append(
                    "resource_query.reject",
                    connection_id=state.connection_id,
                    reason=exc.reason,
                    code=exc.code,
                )
                raise
            if not filtered:
                self.journal.append(
                    "resource_query.reject",
                    connection_id=state.connection_id,
                    reason="no_match",
                    code=int(StatusCode.error_resource_not_found),
                )
                raise errors.VisaIOError(
                    StatusCode.error_resource_not_found
                )
            self.journal.append(
                "resource_query.result",
                connection_id=state.connection_id,
                matched=len(filtered),
            )
            return filtered, None
        if operation == "open":
            self._exact(
                args,
                {"session", "resource_name", "access_mode", "open_timeout"},
            )
            opened, status = library.open(
                session,
                _string(args["resource_name"]),
                AccessModes(_integer(args["access_mode"], minimum=0)),
                _integer(args["open_timeout"], minimum=0),
            )
            return self._token(
                state,
                int(opened),
                session_kind="resource",
                parent=token,
                resource_name=_string(args["resource_name"]),
            ), int(status)
        if operation == "close":
            self._exact(args, {"session"})
            status = library.close(session)
            remote = state.sessions.pop(token)
            self.journal.append(
                "session.close",
                connection_id=state.connection_id,
                session=_audit_token(token),
                session_kind=remote.session_kind,
                source="candidate",
                status=int(status),
            )
            return None, int(status)
        if operation == "read":
            self._exact(args, {"session", "count"})
            count = _integer(args["count"], minimum=1)
            self.journal.append(
                "scpi.read",
                connection_id=state.connection_id,
                session=_audit_token(token),
                count=count,
            )
            result, status = library.read(session, count)
            self.journal.append(
                "scpi.read_result",
                connection_id=state.connection_id,
                session=_audit_token(token),
                payload=_audit_value(bytes(result)),
                status=int(status),
            )
            return bytes(result), int(status)
        if operation == "write":
            self._exact(args, {"session", "data"})
            data = args["data"]
            if not isinstance(data, bytes):
                raise ValueError("data must be bytes")
            self.journal.append(
                "scpi.write",
                connection_id=state.connection_id,
                session=_audit_token(token),
                payload=_audit_value(data),
            )
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            with self.bench.session_context(digest):
                result, status = library.write(session, data)
            self.journal.append(
                "scpi.write_result",
                connection_id=state.connection_id,
                session=_audit_token(token),
                count=int(result),
                status=int(status),
            )
            return int(result), int(status)
        if operation == "get_attribute":
            self._exact(args, {"session", "attribute"})
            result, status = library.get_attribute(
                session, ResourceAttribute(_integer(args["attribute"]))
            )
            return _wire_value(result), int(status)
        if operation == "set_attribute":
            self._exact(
                args, {"session", "attribute", "attribute_state"}
            )
            status = library.set_attribute(
                session,
                ResourceAttribute(_integer(args["attribute"])),
                args["attribute_state"],
            )
            return None, int(status)
        if operation == "disable_event":
            self._exact(
                args, {"session", "event_type", "mechanism"}
            )
            library.disable_event(
                session,
                EventType(_integer(args["event_type"])),
                EventMechanism(_integer(args["mechanism"])),
            )
            return None, None
        self._exact(args, {"session", "event_type", "mechanism"})
        library.discard_events(
            session,
            EventType(_integer(args["event_type"])),
            EventMechanism(_integer(args["mechanism"])),
        )
        return None, None

    def _session(
        self, state: ConnectionState, args: dict[str, WireValue]
    ) -> tuple[str, int]:
        token = args.get("session")
        if not isinstance(token, str) or token not in state.sessions:
            self.journal.append(
                "session.invalid_access",
                connection_id=state.connection_id,
                session=_audit_token(token),
            )
            raise CandidateRequestError(
                "invalid_session", ERROR_INVALID_OBJECT, "invalid session"
            )
        return token, state.sessions[token].real_session

    def _token(
        self,
        state: ConnectionState,
        session: int,
        *,
        session_kind: str,
        parent: str | None = None,
        resource_name: str | None = None,
    ) -> str:
        token = secrets.token_urlsafe(24)
        state.sessions[token] = RemoteSession(session, session_kind)
        fields: dict[str, Any] = {
            "connection_id": state.connection_id,
            "session": _audit_token(token),
            "session_kind": session_kind,
        }
        if parent is not None:
            fields["parent_session"] = _audit_token(parent)
        if resource_name is not None:
            fields["resource_name"] = resource_name
        self.journal.append("session.open", **fields)
        return token

    def _record_reject(
        self,
        state: ConnectionState,
        operation: str,
        error: CandidateRequestError,
        started_ns: int,
    ) -> None:
        self.journal.append(
            "rpc.reject",
            connection_id=state.connection_id,
            operation=operation,
            error_kind=error.kind,
            code=error.code,
            message=error.message,
            latency_ns=max(0, time.monotonic_ns() - started_ns),
        )

    @staticmethod
    def _exact(args: dict[str, WireValue], keys: set[str]) -> None:
        if set(args) != keys:
            raise CandidateRequestError(
                "invalid_arguments", ERROR_INVALID_OBJECT, "invalid arguments"
            )


def _peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    if hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        pid, uid, gid = struct.unpack("3i", raw)
        return uid, gid, pid
    if hasattr(connection, "getpeereid"):
        uid, gid = connection.getpeereid()
        return uid, gid, 0
    return os.getuid(), os.getgid(), 0


def _integer(value: WireValue, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer required")
    if minimum is not None and value < minimum:
        raise ValueError("integer below minimum")
    return value


def _string(value: WireValue) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("string required")
    return value


def _wire_value(value: Any) -> WireValue:
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    if isinstance(value, tuple):
        return tuple(_wire_value(item) for item in value)
    raise RuntimeError("sim returned unsupported wire value")


def _audit_mapping(
    value: dict[str, WireValue], *, redact: bool = False
) -> dict[str, Any]:
    return {
        key: (
            _audit_token(item)
            if key == "session"
            else _audit_value(item, redact=redact)
        )
        for key, item in value.items()
    }


def _audit_token(value: WireValue) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"type": "invalid_token"}
    return {
        "type": "token",
        "length": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _audit_value(value: Any, *, redact: bool = False) -> Any:
    if isinstance(value, bytes):
        audited = {
            "type": "bytes",
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
        if not redact:
            audited["base64"] = base64.b64encode(value).decode("ascii")
        return audited
    if redact and isinstance(value, str):
        payload = value.encode("utf-8")
        return {
            "type": "string",
            "length": len(value),
            "utf8_length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    if isinstance(value, tuple):
        return [_audit_value(item, redact=redact) for item in value]
    if isinstance(value, list):
        return [_audit_value(item, redact=redact) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _audit_value(item, redact=redact)
            for key, item in value.items()
        }
    return value


def _audit_operation(value: str, *, redact: bool) -> Any:
    return _audit_value(value, redact=redact)


def _audit_rejected_mapping(value: dict[str, WireValue]) -> dict[str, Any]:
    keys = json.dumps(
        sorted(value), ensure_ascii=True, separators=(",", ":")
    ).encode()
    return {
        "type": "redacted_mapping",
        "entries": len(value),
        "keys_sha256": hashlib.sha256(keys).hexdigest(),
    }
