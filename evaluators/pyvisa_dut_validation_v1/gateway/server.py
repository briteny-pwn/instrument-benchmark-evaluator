from __future__ import annotations

import base64
import os
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..instruments import CommandError, InstrumentRack
from .journal import EventJournal
from .protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    recv_frame,
    send_frame,
    validate_request,
)


@dataclass
class _Session:
    owner: str
    resource: str
    timeout_ms: int = 5000
    read_termination: str | None = None
    write_termination: str | None = None
    pending: bytes | None = None


class _GatewayOperationError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class GatewayServer:
    def __init__(
        self,
        socket_path: Path,
        rack: InstrumentRack,
        *,
        journal: EventJournal | None = None,
    ):
        self.socket_path = socket_path
        self.rack = rack
        self.journal = journal or EventJournal("unscored-run", rack.spec.world_id)
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._client_threads: set[threading.Thread] = set()
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._last_clock_sync_ns = time.monotonic_ns()

    def start(self) -> None:
        if self._listener is not None:
            raise RuntimeError("gateway already started")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        listener.listen()
        listener.settimeout(0.2)
        self._listener = listener
        self._stop.clear()
        self._last_clock_sync_ns = time.monotonic_ns()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="iab-gateway", daemon=True
        )
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            listener.close()
        thread = self._accept_thread
        if thread is not None:
            thread.join(timeout=2)
        for client_thread in tuple(self._client_threads):
            client_thread.join(timeout=2)
        with self._lock:
            self._sessions.clear()
        self._listener = None
        self._accept_thread = None
        if self.socket_path.exists():
            self.socket_path.unlink()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            owner = secrets.token_urlsafe(18)
            thread = threading.Thread(
                target=self._serve_client,
                args=(connection, owner),
                name=f"iab-client-{owner[:6]}",
                daemon=True,
            )
            self._client_threads.add(thread)
            thread.start()

    def _serve_client(self, connection: socket.socket, owner: str) -> None:
        try:
            with connection:
                while not self._stop.is_set():
                    try:
                        request = recv_frame(connection)
                    except (EOFError, ConnectionError, ProtocolError):
                        break
                    try:
                        request_id, operation, arguments = validate_request(request)
                        result = self._dispatch(owner, operation, arguments)
                        response = {
                            "version": PROTOCOL_VERSION,
                            "request_id": request_id,
                            "ok": True,
                            "result": result,
                        }
                    except ProtocolError as exc:
                        request_id = str(request.get("request_id", "invalid"))
                        response = self._error(request_id, exc.code, str(exc))
                    except _GatewayOperationError as exc:
                        request_id = str(request.get("request_id", "invalid"))
                        response = self._error(request_id, exc.code, exc.message)
                    except (CommandError, KeyError) as exc:
                        request_id = str(request.get("request_id", "invalid"))
                        response = self._error(request_id, "instrument_error", str(exc))
                    try:
                        send_frame(connection, response)
                    except (OSError, ProtocolError):
                        break
        finally:
            self._force_close_owner(owner)
            self._client_threads.discard(threading.current_thread())

    @staticmethod
    def _error(request_id: str, code: str, message: str) -> dict[str, Any]:
        return {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        }

    def _dispatch(
        self, owner: str, operation: str, arguments: dict[str, Any]
    ) -> Any:
        self._sync_clock()
        if operation == "protocol_version":
            return PROTOCOL_VERSION
        if operation == "list_resources":
            snapshot = self.rack.world.snapshot()
            resources = list(self.rack.list_resources())
            self.journal.append(
                operation="list_resources",
                state_before=snapshot,
                state_after=snapshot,
                response="\n".join(resources).encode(),
            )
            return resources
        if operation == "open_resource":
            resource = self._required_str(arguments, "resource")
            if resource not in self.rack.list_resources():
                raise _GatewayOperationError(
                    "resource_not_found", f"unknown resource {resource!r}"
                )
            session_id = secrets.token_urlsafe(24)
            with self._lock:
                self._sessions[session_id] = _Session(owner=owner, resource=resource)
            self.rack.world.note_session_reopen(self.rack.role_for(resource))
            snapshot = self.rack.world.snapshot()
            self.journal.append(
                operation="open_resource",
                state_before=snapshot,
                state_after=snapshot,
                session=session_id,
                resource=resource,
                role=self.rack.role_for(resource),
                request=resource.encode(),
            )
            return session_id

        session_id = self._required_str(arguments, "session")
        session = self._owned_session(owner, session_id)
        before = self.rack.world.snapshot()
        if operation == "set_timeout":
            timeout_ms = arguments.get("timeout_ms")
            if not isinstance(timeout_ms, int) or timeout_ms < 1 or timeout_ms > 120_000:
                raise _GatewayOperationError(
                    "invalid_argument", "timeout_ms must be between 1 and 120000"
                )
            session.timeout_ms = timeout_ms
            self._record_session(
                operation, session_id, session, before, request=str(timeout_ms).encode()
            )
            return True
        if operation == "set_read_termination":
            session.read_termination = self._termination(arguments)
            self._record_session(
                operation,
                session_id,
                session,
                before,
                request=str(session.read_termination).encode(),
            )
            return True
        if operation == "set_write_termination":
            session.write_termination = self._termination(arguments)
            self._record_session(
                operation,
                session_id,
                session,
                before,
                request=str(session.write_termination).encode(),
            )
            return True
        if operation == "write":
            payload = self._decode_payload(arguments)
            if session.write_termination:
                ending = session.write_termination.encode("ascii")
                if not payload.endswith(ending):
                    payload += ending
            try:
                response = self.rack.write(session.resource, payload)
            except (CommandError, KeyError) as exc:
                self._record_session(
                    operation,
                    session_id,
                    session,
                    before,
                    request=payload,
                    outcome="error",
                    error_code="instrument_error",
                )
                raise
            if response is not None:
                if session.pending is not None:
                    raise _GatewayOperationError(
                        "pending_response", "read the pending response before another query"
                    )
                session.pending = response
            self._record_session(
                operation,
                session_id,
                session,
                before,
                request=payload,
                response=response,
            )
            return {"bytes_written": len(payload)}
        if operation == "read":
            if session.pending is None:
                raise _GatewayOperationError(
                    "no_pending_response", "session has no pending response"
                )
            payload = session.pending
            session.pending = None
            self._record_session(
                operation,
                session_id,
                session,
                before,
                response=payload,
            )
            return {"payload_b64": base64.b64encode(payload).decode("ascii")}
        if operation == "close":
            self._record_session(
                operation,
                session_id,
                session,
                before,
                cleanup_source="candidate",
            )
            with self._lock:
                del self._sessions[session_id]
            return True
        raise _GatewayOperationError(
            "unsupported_operation", f"unsupported operation {operation!r}"
        )

    def _owned_session(self, owner: str, session_id: str) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.owner != owner:
            raise _GatewayOperationError("invalid_session", "invalid or foreign session")
        return session

    def _force_close_owner(self, owner: str) -> None:
        with self._lock:
            leaked = [
                (session_id, session)
                for session_id, session in self._sessions.items()
                if session.owner == owner
            ]
            for session_id, session in leaked:
                snapshot = self.rack.world.snapshot()
                self._record_session(
                    "close",
                    session_id,
                    session,
                    snapshot,
                    cleanup_source="forced",
                )
                del self._sessions[session_id]

    def _record_session(
        self,
        operation: str,
        session_id: str,
        session: _Session,
        before,
        *,
        request: bytes | None = None,
        response: bytes | None = None,
        cleanup_source: str | None = None,
        outcome: str = "ok",
        error_code: str | None = None,
    ) -> None:
        self.journal.append(
            operation=operation,
            state_before=before,
            state_after=self.rack.world.snapshot(),
            session=session_id,
            resource=session.resource,
            role=self.rack.role_for(session.resource),
            request=request,
            response=response,
            cleanup_source=cleanup_source,
            outcome=outcome,
            error_code=error_code,
        )

    def _sync_clock(self) -> None:
        now = time.monotonic_ns()
        with self._lock:
            elapsed_ms = (now - self._last_clock_sync_ns) // 1_000_000
            if elapsed_ms > 0:
                self.rack.world.advance_ms(int(elapsed_ms))
                self._last_clock_sync_ns += int(elapsed_ms) * 1_000_000

    @staticmethod
    def _required_str(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value:
            raise _GatewayOperationError("invalid_argument", f"{key} is required")
        return value

    @staticmethod
    def _termination(arguments: dict[str, Any]) -> str | None:
        value = arguments.get("termination")
        if value is None:
            return None
        normalized = {
            "LF": "\n",
            "CR": "\r",
            "CRLF": "\r\n",
            "\n": "\n",
            "\r": "\r",
            "\r\n": "\r\n",
        }.get(value)
        if normalized is None:
            raise _GatewayOperationError(
                "invalid_argument",
                "termination must be LF, CR, CRLF, newline, carriage return, or both",
            )
        return normalized

    @staticmethod
    def _decode_payload(arguments: dict[str, Any]) -> bytes:
        value = arguments.get("payload_b64")
        if not isinstance(value, str):
            raise _GatewayOperationError("invalid_argument", "payload_b64 is required")
        try:
            payload = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise _GatewayOperationError(
                "invalid_argument", "payload_b64 is invalid"
            ) from exc
        if len(payload) > 262_144:
            raise _GatewayOperationError("payload_too_large", "payload exceeds 262144 bytes")
        return payload
