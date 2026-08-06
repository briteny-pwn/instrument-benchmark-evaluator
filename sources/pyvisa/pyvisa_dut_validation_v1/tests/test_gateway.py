from __future__ import annotations

import socket
import struct
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from sources.pyvisa.pyvisa_dut_validation_v1.gateway.server import GatewayServer
from sources.pyvisa.pyvisa_dut_validation_v1.instruments import (
    DMM_RESOURCE,
    PSU_RESOURCE,
    SCOPE_RESOURCE,
    InstrumentRack,
)
from sources.pyvisa.pyvisa_dut_validation_v1.models import WorldSpec
from tests.fixtures.instance.starter.gateway_client import (
    GatewayClient,
    GatewayError,
)

from sources.pyvisa.pyvisa_dut_validation_v1.tests.test_instruments import ready_rack


@contextmanager
def running_gateway(rack: InstrumentRack, *, journal=None):
    with tempfile.TemporaryDirectory() as directory:
        endpoint = Path(directory) / "gateway.sock"
        server = GatewayServer(endpoint, rack, journal=journal)
        server.start()
        try:
            yield endpoint, server
        finally:
            server.stop()
            rack.close()


class GatewayTests(unittest.TestCase):
    def test_symbolic_termination_names_are_accepted(self) -> None:
        with running_gateway(InstrumentRack(WorldSpec.nominal())) as (endpoint, _):
            with GatewayClient(endpoint) as client:
                for symbolic in ("LF", "CR", "CRLF"):
                    with self.subTest(symbolic=symbolic):
                        session = client.open_resource(PSU_RESOURCE)
                        client.set_read_termination(session, symbolic)
                        client.set_write_termination(session, symbolic)
                        client.write(session, b"*IDN?")
                        identity = client.read(session)
                        client.close_resource(session)
                        self.assertIn(b"Virtual-E36312A", identity)

    def test_lists_opens_configures_queries_and_closes(self) -> None:
        with running_gateway(InstrumentRack(WorldSpec.nominal())) as (endpoint, _):
            with GatewayClient(endpoint) as client:
                resources = client.list_resources()
                session = client.open_resource(PSU_RESOURCE)
                client.set_timeout(session, 2000)
                client.set_read_termination(session, "\n")
                client.set_write_termination(session, "\n")
                client.write(session, b"*IDN?")
                identity = client.read(session)
                client.close_resource(session)

        self.assertEqual(len(resources), 5)
        self.assertIn(b"Virtual-E36312A", identity)

    def test_binary_read_round_trip(self) -> None:
        rack = ready_rack()
        with running_gateway(rack) as (endpoint, _):
            with GatewayClient(endpoint) as client:
                session = client.open_resource(SCOPE_RESOURCE)
                client.set_write_termination(session, "\n")
                for command in (
                    b"DATA:SOURCE CH1",
                    b"DATA:ENC RIBINARY",
                    b"DATA:WIDTH 1",
                    b"CURVE?",
                ):
                    client.write(session, command)
                response = client.read(session)
                client.close_resource(session)

        self.assertTrue(response.startswith(b"#"))
        self.assertIn(b"\x00", response)

    def test_read_without_pending_response_is_typed_error(self) -> None:
        with running_gateway(InstrumentRack(WorldSpec.nominal())) as (endpoint, _):
            with GatewayClient(endpoint) as client:
                session = client.open_resource(DMM_RESOURCE)
                with self.assertRaisesRegex(GatewayError, "no_pending_response"):
                    client.read(session)
                client.close_resource(session)

    def test_use_after_close_is_typed_error(self) -> None:
        with running_gateway(InstrumentRack(WorldSpec.nominal())) as (endpoint, _):
            with GatewayClient(endpoint) as client:
                session = client.open_resource(PSU_RESOURCE)
                client.close_resource(session)
                with self.assertRaisesRegex(GatewayError, "invalid_session"):
                    client.write(session, b"*IDN?\n")

    def test_session_cannot_be_used_by_another_client(self) -> None:
        with running_gateway(InstrumentRack(WorldSpec.nominal())) as (endpoint, _):
            with GatewayClient(endpoint) as owner, GatewayClient(endpoint) as stranger:
                session = owner.open_resource(PSU_RESOURCE)
                with self.assertRaisesRegex(GatewayError, "invalid_session"):
                    stranger.write(session, b"*IDN?\n")
                owner.close_resource(session)

    def test_parallel_sessions_do_not_cross_pending_responses(self) -> None:
        with running_gateway(InstrumentRack(WorldSpec.nominal())) as (endpoint, _):
            identities: dict[str, bytes] = {}

            def query(resource: str) -> None:
                with GatewayClient(endpoint) as client:
                    session = client.open_resource(resource)
                    client.write(session, b"*IDN?\n")
                    identities[resource] = client.read(session)
                    client.close_resource(session)

            threads = [
                threading.Thread(target=query, args=(PSU_RESOURCE,)),
                threading.Thread(target=query, args=(DMM_RESOURCE,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertIn(b"Virtual-E36312A", identities[PSU_RESOURCE])
        self.assertIn(b"Virtual-DMM7510", identities[DMM_RESOURCE])

    def test_oversized_frame_is_rejected_without_server_exit(self) -> None:
        with running_gateway(InstrumentRack(WorldSpec.nominal())) as (endpoint, _):
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.connect(str(endpoint))
            raw.sendall(struct.pack(">I", 2_000_000))
            raw.close()

            with GatewayClient(endpoint) as client:
                self.assertEqual(client.protocol_version(), 1)
                self.assertEqual(len(client.list_resources()), 5)


if __name__ == "__main__":
    unittest.main()
