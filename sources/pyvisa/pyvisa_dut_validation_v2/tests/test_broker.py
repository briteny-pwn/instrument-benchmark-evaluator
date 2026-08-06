from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from pyvisa.constants import EventMechanism, EventType, StatusCode

from sources.pyvisa.pyvisa_dut_validation_v2.broker import (
    OPERATIONS,
    CandidateRequestError,
    RemoteVisaBroker,
)
from sources.pyvisa.pyvisa_dut_validation_v2.journal import EventJournal
from sources.pyvisa.pyvisa_dut_validation_v2.protocol import (
    decode_response,
    encode_request,
    recv_message,
)


class FakeDevices:
    def list_resources(self):
        return ("TCPIP::scope::INSTR",)


class FakeVisaLibrary:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed: list[int] = []
        self.timeout = 2000
        self.devices = FakeDevices()

    def open_default_resource_manager(self):
        self.calls.append(("open_default_resource_manager", ()))
        return 101, StatusCode.success

    def open(self, session, name, access_mode, timeout):
        self.calls.append(("open", (session, name, access_mode, timeout)))
        return 202, StatusCode.success

    def close(self, session):
        self.calls.append(("close", (session,)))
        self.closed.append(session)
        return StatusCode.success

    def write(self, session, data):
        self.calls.append(("write", (session, data)))
        return len(data), StatusCode.success

    def read(self, session, count):
        self.calls.append(("read", (session, count)))
        return b"IAB\n", StatusCode.success

    def get_attribute(self, session, attribute):
        self.calls.append(("get_attribute", (session, attribute)))
        return self.timeout, StatusCode.success

    def set_attribute(self, session, attribute, value):
        self.calls.append(("set_attribute", (session, attribute, value)))
        self.timeout = value
        return StatusCode.success

    def disable_event(self, session, event_type, mechanism):
        self.calls.append(("disable_event", (session, event_type, mechanism)))
        return None

    def discard_events(self, session, event_type, mechanism):
        self.calls.append(("discard_events", (session, event_type, mechanism)))
        return None


class FakeBench:
    def __init__(self) -> None:
        self.visalib = FakeVisaLibrary()
        self.session_digests: list[str] = []
        self.cancelled = False

    def cancel_operations(self):
        self.cancelled = True

    def session_context(self, digest):
        bench = self

        class Context:
            def __enter__(self):
                bench.session_digests.append(digest)

            def __exit__(self, *args):
                return False

        return Context()


class RemoteVisaBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = EventJournal("run", "world")
        self.bench = FakeBench()
        self.broker = RemoteVisaBroker(self.bench, self.journal)
        self.state = self.broker.new_connection(10001, 10001, 123)

    def test_exact_operation_surface_and_all_return_shapes(self) -> None:
        self.assertEqual(len(OPERATIONS), 10)
        rm_token, status = self.broker.dispatch(
            self.state, "open_default_resource_manager", {}
        )
        self.assertIsInstance(rm_token, str)
        self.assertNotEqual(rm_token, "101")
        self.assertEqual(status, 0)
        resources, status = self.broker.dispatch(
            self.state,
            "list_resources",
            {"session": rm_token, "query": "?*::INSTR"},
        )
        self.assertEqual(resources, ("TCPIP::scope::INSTR",))
        self.assertIsNone(status)
        resource_token, status = self.broker.dispatch(
            self.state,
            "open",
            {
                "session": rm_token,
                "resource_name": resources[0],
                "access_mode": 0,
                "open_timeout": 0,
            },
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            self.broker.dispatch(
                self.state,
                "write",
                {"session": resource_token, "data": b"*IDN?\n"},
            ),
            (6, 0),
        )
        self.assertEqual(
            self.broker.dispatch(
                self.state,
                "read",
                {"session": resource_token, "count": 4096},
            ),
            (b"IAB\n", 0),
        )
        self.assertEqual(
            self.broker.dispatch(
                self.state,
                "get_attribute",
                {"session": resource_token, "attribute": 1073676314},
            ),
            (2000, 0),
        )
        self.assertEqual(
            self.broker.dispatch(
                self.state,
                "set_attribute",
                {
                    "session": resource_token,
                    "attribute": 1073676314,
                    "attribute_state": 5000,
                },
            ),
            (None, 0),
        )
        for operation in ("disable_event", "discard_events"):
            self.assertEqual(
                self.broker.dispatch(
                    self.state,
                    operation,
                    {
                        "session": resource_token,
                        "event_type": int(EventType.service_request),
                        "mechanism": int(EventMechanism.queue),
                    },
                ),
                (None, None),
            )
        self.assertEqual(
            self.broker.dispatch(
                self.state, "close", {"session": resource_token}
            ),
            (None, 0),
        )
        self.assertEqual(
            self.broker.dispatch(self.state, "close", {"session": rm_token}),
            (None, 0),
        )
        event_args = [
            event.fields.get("args")
            for event in self.journal.events
            if event.kind == "rpc.request"
        ]
        self.assertTrue(any("data" in args for args in event_args if args))
        self.assertNotIn(b"*IDN?\n", repr(event_args).encode())
        kinds = [event.kind for event in self.journal.events]
        self.assertEqual(kinds.count("session.open"), 2)
        self.assertEqual(kinds.count("session.close"), 2)
        self.assertEqual(kinds.count("scpi.write"), 1)
        self.assertEqual(kinds.count("scpi.read"), 1)
        write = next(event for event in self.journal.events if event.kind == "scpi.write")
        self.assertEqual(write.fields["payload"]["length"], 6)
        read = next(
            event for event in self.journal.events
            if event.kind == "scpi.read_result"
        )
        self.assertEqual(read.fields["payload"]["base64"], "SUFCCg==")
        self.assertEqual(kinds.count("scpi.write_result"), 1)
        self.assertEqual(kinds.count("scpi.read_result"), 1)
        for event in self.journal.events:
            if event.kind in {"rpc.result", "rpc.reject"}:
                self.assertIsInstance(event.fields.get("latency_ns"), int)
                self.assertGreaterEqual(event.fields["latency_ns"], 0)

    def test_tokens_are_connection_owned_and_disconnect_closes_only_owner(self) -> None:
        token, _ = self.broker.dispatch(
            self.state, "open_default_resource_manager", {}
        )
        other = self.broker.new_connection(10001, 10001, 456)
        with self.assertRaisesRegex(CandidateRequestError, "invalid session"):
            self.broker.dispatch(
                other, "list_resources", {"session": token, "query": "?*"}
            )
        self.broker.disconnect(other)
        self.assertEqual(self.bench.visalib.closed, [])
        self.broker.disconnect(self.state)
        self.assertEqual(self.bench.visalib.closed, [101])
        kinds = [event.kind for event in self.journal.events]
        self.assertIn("session.invalid_access", kinds)
        forced = [
            event for event in self.journal.events
            if event.kind == "session.forced_cleanup"
        ]
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0].fields["session_kind"], "resource_manager")
        self.assertEqual(self.broker.freeze_and_close().leaked_sessions, 1)

    def test_clean_peer_eof_is_normal_close_not_protocol_reject(self) -> None:
        server, client = socket.socketpair()
        state = self.broker.new_connection(10001, 10001, 777)
        worker = threading.Thread(
            target=self.broker._serve_connection,
            args=(server, state),
        )
        worker.start()
        client.close()
        worker.join(2.0)
        self.assertFalse(worker.is_alive())
        kinds = [event.kind for event in self.journal.events]
        self.assertNotIn("connection.reject", kinds)
        self.assertEqual(kinds[-1], "connection.close")

    def test_candidate_json_value_failures_are_protocol_rejections(self) -> None:
        payloads = (
            json.dumps(
                {
                    "version": 1,
                    "request_id": 1,
                    "operation": "\ud800",
                    "args": {},
                }
            ).encode(),
            (
                b'{"version":1,"request_id":1,"operation":"hello",'
                b'"args":{"value":'
                + b"1" * 5000
                + b"}}"
            ),
        )
        for payload in payloads:
            server, client = socket.socketpair()
            state = self.broker.new_connection(10001, 10001, 778)
            worker = threading.Thread(
                target=self.broker._serve_connection,
                args=(server, state),
            )
            worker.start()
            client.sendall(len(payload).to_bytes(4, "big") + payload)
            client.close()
            worker.join(2.0)
            self.assertFalse(worker.is_alive())
        kinds = [event.kind for event in self.journal.events]
        self.assertEqual(kinds.count("connection.reject"), 2)
        self.assertNotIn("trusted.failure_detected", kinds)
        self.assertNotIn("trusted.fatal", kinds)

    def test_readiness_uid_can_only_hello_and_bad_requests_are_not_fatal(self) -> None:
        ready = self.broker.new_connection(11001, 11001, 1)
        operations, status = self.broker.dispatch(ready, "hello", {})
        self.assertEqual(set(operations), OPERATIONS)
        self.assertIsNone(status)
        with self.assertRaises(CandidateRequestError):
            self.broker.dispatch(ready, "open_default_resource_manager", {})
        with self.assertRaises(CandidateRequestError):
            self.broker.dispatch(self.state, "trigger", {})
        self.assertNotIn("trusted.fatal", {event.kind for event in self.journal.events})

    def test_connection_and_request_limits_are_candidate_rejections(self) -> None:
        broker = RemoteVisaBroker(
            self.bench,
            self.journal,
            max_connections=1,
            max_total_connections=2,
            max_requests_per_connection=1,
            max_total_requests=2,
        )
        first = broker.new_connection(10001, 10001, 10)
        with self.assertRaisesRegex(CandidateRequestError, "connection limit"):
            broker.new_connection(10001, 10001, 11)
        broker.dispatch(first, "hello", {})
        with self.assertRaisesRegex(CandidateRequestError, "request limit"):
            broker.dispatch(first, "hello", {})
        kinds = [event.kind for event in self.journal.events]
        self.assertIn("connection.reject", kinds)
        self.assertIn("rpc.reject", kinds)
        self.assertNotIn("trusted.fatal", kinds)
        broker.disconnect(first)
        replacement = broker.new_connection(10001, 10001, 12)
        self.assertFalse(replacement.closed)
        broker.disconnect(replacement)
        with self.assertRaisesRegex(CandidateRequestError, "connection limit"):
            broker.new_connection(10001, 10001, 13)
        self.assertEqual(broker.freeze_and_close().connections, 2)

        request_limited = RemoteVisaBroker(
            self.bench,
            EventJournal("run", "request-limit"),
            max_connections=2,
            max_total_connections=2,
            max_requests_per_connection=10,
            max_total_requests=1,
        )
        first = request_limited.new_connection(10001)
        second = request_limited.new_connection(10001)
        request_limited.dispatch(first, "hello", {})
        with self.assertRaisesRegex(CandidateRequestError, "request limit"):
            request_limited.dispatch(second, "hello", {})

        byte_limited_journal = EventJournal("run", "byte-limit")
        byte_limited = RemoteVisaBroker(
            self.bench,
            byte_limited_journal,
            max_total_argument_bytes=5,
        )
        state = byte_limited.new_connection(10001)
        with self.assertRaisesRegex(CandidateRequestError, "request limit"):
            byte_limited.dispatch(state, "hello", {"junk": "secret-value"})
        request = next(
            event
            for event in byte_limited_journal.events
            if event.kind == "rpc.request"
        )
        self.assertNotIn("secret-value", repr(request.fields))
        self.assertEqual(request.fields["args"]["type"], "redacted_mapping")
        self.assertEqual(request.fields["args"]["entries"], 1)

    def test_freeze_requests_finalization_cancellation(self) -> None:
        summary = self.broker.freeze_and_close()
        self.assertTrue(summary.frozen)
        self.assertTrue(self.bench.cancelled)
        cancellation = next(
            event for event in self.journal.events
            if event.kind == "broker.cancellation_requested"
        )
        self.assertEqual(cancellation.fields["active_workers"], 0)
        self.assertEqual(cancellation.fields["active_connections"], 1)

    def test_default_multikey_budget_keeps_rejected_journal_bounded(self) -> None:
        journal = EventJournal("run", "multikey-budget")
        broker = RemoteVisaBroker(self.bench, journal)
        state = broker.new_connection(10001)
        args = {f"key_{index:04d}": None for index in range(2048)}
        for _ in range(broker.max_total_requests):
            with self.assertRaises(CandidateRequestError):
                broker.dispatch(state, "hello", args)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            journal.export(path)
            self.assertLess(path.stat().st_size, 16 * 1024 * 1024)
        redacted = [
            event
            for event in journal.events
            if event.kind == "rpc.request"
            and event.fields["args"].get("type") == "redacted_mapping"
        ]
        self.assertTrue(redacted)
        self.assertTrue(
            all(event.fields["args"]["entries"] == 2048 for event in redacted)
        )

    def test_exact_arguments_reject_bool_and_unknown_fields(self) -> None:
        token, _ = self.broker.dispatch(
            self.state, "open_default_resource_manager", {}
        )
        cases = (
            ("read", {"session": token, "count": True}),
            ("read", {"session": token, "count": 1, "extra": 2}),
            ("get_attribute", {"session": token}),
        )
        for operation, args in cases:
            with self.subTest(operation=operation, args=args):
                with self.assertRaises(CandidateRequestError):
                    self.broker.dispatch(self.state, operation, args)

    def test_real_unix_server_frames_requests_and_cleans_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            endpoint = Path(directory) / "transport" / "visa.sock"
            stop = threading.Event()
            worker = threading.Thread(
                target=self.broker.serve_unix,
                args=(endpoint, stop),
                kwargs={
                    "candidate_uid": os.getuid(),
                    "readiness_uid": os.getuid(),
                },
            )
            worker.start()
            self.assertTrue(self.broker.wait_ready(2.0))
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(endpoint))
            try:
                client.sendall(encode_request(1, "hello", {}))
                result, status = decode_response(recv_message(client), 1)
                self.assertEqual(set(result), OPERATIONS)
                self.assertIsNone(status)
            finally:
                client.close()
                stop.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(endpoint.exists())


if __name__ == "__main__":
    unittest.main()
