from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
import struct
import threading
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
    ProtocolError,
    WireValue,
    decode_request,
    encode_message,
    error_response,
    recv_message,
    success_response,
)


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
class ConnectionState:
    connection_id: str
    peer_uid: int
    peer_gid: int
    peer_pid: int
    sessions: dict[str, int] = field(default_factory=dict)
    closed: bool = False


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
    def __init__(self, bench: Any, journal: EventJournal) -> None:
        self.bench = bench
        self.journal = journal
        self._states: list[ConnectionState] = []
        self._workers: list[threading.Thread] = []
        self._state_lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._ready = threading.Event()
        self._frozen = False

    def new_connection(
        self, uid: int, gid: int = 0, pid: int = 0
    ) -> ConnectionState:
        state = ConnectionState(secrets.token_hex(16), uid, gid, pid)
        with self._state_lock:
            if self._frozen:
                raise RuntimeError("broker is frozen")
            self._states.append(state)
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
        self.journal.append(
            "rpc.request",
            connection_id=state.connection_id,
            operation=operation,
            args=_audit_mapping(args),
        )
        try:
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
            )
            return result, status
        except CandidateRequestError as exc:
            self._record_reject(state, operation, exc)
            raise
        except errors.VisaIOError as exc:
            rejection = CandidateRequestError(
                "visa_error", int(exc.error_code), "VISA operation failed"
            )
            self._record_reject(state, operation, rejection)
            raise rejection from None
        except CommandRejected as exc:
            rejection = CandidateRequestError(
                "visa_error", exc.code, "VISA operation failed"
            )
            self._record_reject(state, operation, rejection)
            raise rejection from None
        except (TypeError, ValueError, KeyError) as exc:
            rejection = CandidateRequestError(
                "invalid_arguments", ERROR_INVALID_OBJECT, "invalid arguments"
            )
            self._record_reject(state, operation, rejection)
            raise rejection from exc
        except BaseException as exc:
            self.journal.append(
                "trusted.fatal",
                component="broker",
                exception_type=type(exc).__name__,
                message="broker invariant failure",
            )
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
            listener.settimeout(0.1)
            self._ready.set()
            self.journal.append("broker.ready", endpoint_name=endpoint.name)
            while not stop_event.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                uid, gid, pid = _peer_credentials(connection)
                effective_uid = 10001 if uid == candidate_uid else 11001 if uid == readiness_uid else uid
                state = self.new_connection(effective_uid, gid, pid)
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
            for worker in workers:
                worker.join(2.0)
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
            for session in tuple(reversed(tuple(state.sessions.values()))):
                self.bench.visalib.close(session)
            state.sessions.clear()
            state.closed = True
        self.journal.append(
            "connection.close",
            connection_id=state.connection_id,
            leaked_sessions=leaked,
        )
        return leaked

    def freeze_and_close(self) -> BrokerSummary:
        with self._state_lock:
            self._frozen = True
            states = tuple(self._states)
            workers = tuple(self._workers)
        for worker in workers:
            worker.join(2.0)
        leaked = sum(self.disconnect(state) for state in states)
        summary = BrokerSummary(len(states), leaked, True)
        self.journal.append(
            "broker.frozen",
            connections=summary.connections,
            leaked_sessions=summary.leaked_sessions,
        )
        return summary

    def _serve_connection(
        self, connection: socket.socket, state: ConnectionState
    ) -> None:
        try:
            while True:
                try:
                    message = recv_message(connection)
                    request_id, operation, args = decode_request(message)
                    try:
                        result, status = self.dispatch(state, operation, args)
                        response = success_response(request_id, result, status)
                    except CandidateRequestError as exc:
                        response = error_response(
                            request_id, exc.kind, exc.code, exc.message
                        )
                    connection.sendall(encode_message(response))
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
        finally:
            connection.close()
            self.disconnect(state)

    def _invoke(
        self,
        state: ConnectionState,
        operation: str,
        args: dict[str, WireValue],
    ) -> tuple[WireValue, int | None]:
        library = self.bench.visalib
        if operation == "open_default_resource_manager":
            self._exact(args, set())
            session, status = library.open_default_resource_manager()
            return self._token(state, int(session)), int(status)

        token, session = self._session(state, args)
        if operation == "list_resources":
            self._exact(args, {"session", "query"})
            query = _string(args["query"])
            return tuple(library.list_resources(session, query)), None
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
            return self._token(state, int(opened)), int(status)
        if operation == "close":
            self._exact(args, {"session"})
            status = library.close(session)
            state.sessions.pop(token)
            return None, int(status)
        if operation == "read":
            self._exact(args, {"session", "count"})
            result, status = library.read(
                session, _integer(args["count"], minimum=1)
            )
            return bytes(result), int(status)
        if operation == "write":
            self._exact(args, {"session", "data"})
            data = args["data"]
            if not isinstance(data, bytes):
                raise ValueError("data must be bytes")
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            with self.bench.session_context(digest):
                result, status = library.write(session, data)
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
            raise CandidateRequestError(
                "invalid_session", ERROR_INVALID_OBJECT, "invalid session"
            )
        return token, state.sessions[token]

    def _token(self, state: ConnectionState, session: int) -> str:
        token = secrets.token_urlsafe(24)
        state.sessions[token] = session
        return token

    def _record_reject(
        self,
        state: ConnectionState,
        operation: str,
        error: CandidateRequestError,
    ) -> None:
        self.journal.append(
            "rpc.reject",
            connection_id=state.connection_id,
            operation=operation,
            error_kind=error.kind,
            code=error.code,
            message=error.message,
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


def _audit_mapping(value: dict[str, WireValue]) -> dict[str, Any]:
    return {
        key: _audit_token(item) if key == "session" else _audit_value(item)
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


def _audit_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, tuple):
        return [_audit_value(item) for item in value]
    if isinstance(value, list):
        return [_audit_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _audit_value(item) for key, item in value.items()}
    return value
