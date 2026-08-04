from __future__ import annotations

import base64
import time
import unittest
from dataclasses import replace
from pathlib import Path

from pyvisa.constants import ResourceAttribute

from evaluators.pyvisa_dut_validation_v1.models import WorldSpec
from evaluators.pyvisa_dut_validation_v2.bench import BenchContext
from evaluators.pyvisa_dut_validation_v2.broker import (
    CandidateRequestError,
    RemoteVisaBroker,
)
from evaluators.pyvisa_dut_validation_v2.journal import EventJournal
from evaluators.pyvisa_dut_validation_v2.projection import project_events


ROOT = Path(__file__).resolve().parents[3]
SIMULATOR = ROOT / "evaluators" / "pyvisa_dut_validation_v2" / "simulator.yaml"
POINTS = (0.0, 0.3, 0.6, 0.9, 1.2, 0.9, 0.6, 0.3)


def raw_nominal(
    *,
    force_scope_close: bool = False,
    reject: bool = False,
    transient: bool = False,
    extra_scope_leaks: int = 0,
):
    spec = replace(WorldSpec.nominal(seed=29), settle_ms=1)
    if transient:
        spec = replace(
            spec,
            transient_error_role="dmm",
            transient_error_command="*IDN?",
            transient_error_count=1,
        )
    journal = EventJournal("projection-run", spec.world_id)
    bench = BenchContext.from_world(SIMULATOR, spec, journal)
    broker = RemoteVisaBroker(bench, journal)
    state = broker.new_connection(10001, 10001, 29)
    manager, _ = broker.dispatch(state, "open_default_resource_manager", {})
    resources, _ = broker.dispatch(
        state,
        "list_resources",
        {"session": manager, "query": "?*::INSTR"},
    )
    by_resource = {resource: role for role, resource in spec.resource_map}
    sessions: dict[str, str] = {}
    for resource in resources:
        role = by_resource.get(resource)
        if role is None:
            continue
        session, _ = broker.dispatch(
            state,
            "open",
            {
                "session": manager,
                "resource_name": resource,
                "access_mode": 0,
                "open_timeout": 0,
            },
        )
        sessions[role] = session
        for attribute, value in (
            (ResourceAttribute.timeout_value, 5000),
            (ResourceAttribute.termchar, 10),
            (ResourceAttribute.termchar_enabled, True),
        ):
            broker.dispatch(
                state,
                "set_attribute",
                {
                    "session": session,
                    "attribute": int(attribute),
                    "attribute_state": value,
                },
            )
    for _ in range(extra_scope_leaks):
        broker.dispatch(
            state,
            "open",
            {
                "session": manager,
                "resource_name": dict(spec.resource_map)["scope"],
                "access_mode": 0,
                "open_timeout": 0,
            },
        )

    def write(role: str, command: str) -> None:
        payload = command.encode("ascii") + b"\n"
        broker.dispatch(
            state, "write", {"session": sessions[role], "data": payload}
        )

    def query(role: str, command: str) -> bytes:
        write(role, command)
        value, _ = broker.dispatch(
            state, "read", {"session": sessions[role], "count": 1_048_576}
        )
        return value

    identities = {}
    for role in ("psu", "switch", "awg", "scope", "dmm"):
        try:
            identity = query(role, "*IDN?")
        except CandidateRequestError:
            if not transient or role != "dmm":
                raise
            identity = query(role, "*IDN?")
        identities[role] = identity.decode("ascii").strip()
    write("switch", "ROUT:CLOS (@1101,1102)")
    write("psu", "VOLT 5.0")
    write("psu", "OUTP ON")
    write("awg", "DATA:ARB DUT_STAIR," + ",".join(map(str, POINTS)))
    write("awg", "FUNC:ARB DUT_STAIR")
    write("awg", "VOLT 1.2")
    write("awg", "VOLT:OFFS 0")
    write("awg", "OUTP ON")
    time.sleep(0.003)
    query("awg", "STAT:OPER:COND?")
    write("dmm", "CONF:VOLT:DC")
    write("dmm", "VOLT:DC:RANG 10")
    write("dmm", "SAMP:COUN 8")
    write("dmm", "INIT")
    query("dmm", "READ?")
    write("scope", "DATA:SOURCE CH1")
    write("scope", "DATA:ENC RIBINARY")
    write("scope", "DATA:WIDTH 1")
    query("scope", "WFMOUTPRE:YMULT?")
    query("scope", "WFMOUTPRE:YOFF?")
    query("scope", "WFMOUTPRE:YZERO?")
    query("scope", "CURVE?")
    write("awg", "OUTP OFF")
    write("psu", "OUTP OFF")
    write("switch", "ROUT:OPEN:ALL")
    final_snapshot = bench.snapshot()
    for role, session in sessions.items():
        if force_scope_close and role == "scope":
            continue
        broker.dispatch(state, "close", {"session": session})
    if reject:
        with unittest.TestCase().assertRaises(CandidateRequestError):
            broker.dispatch(state, "not-a-visa-operation", {})
        with unittest.TestCase().assertRaises(CandidateRequestError):
            broker.dispatch(
                state,
                "write",
                {"session": True, "data": 7},
            )
        journal.append(
            "connection.reject",
            connection_id=state.connection_id,
            reason="malformed_frame",
            detail="invalid frame length",
        )
    broker.dispatch(state, "close", {"session": manager})
    broker.disconnect(state)
    bench.force_safe()
    bench.close()
    return (
        spec,
        journal.events,
        final_snapshot,
        identities,
        dict(spec.resource_map),
    )


def raw_dirty_initial_state():
    spec = replace(
        WorldSpec.nominal(seed=30),
        initial_psu_output=True,
        initial_awg_output=True,
        initial_closed_routes=frozenset({"1199"}),
    )
    journal = EventJournal("dirty-run", spec.world_id)
    bench = BenchContext.from_world(SIMULATOR, spec, journal)
    broker = RemoteVisaBroker(bench, journal)
    state = broker.new_connection(10001, 10001, 30)
    manager, _ = broker.dispatch(state, "open_default_resource_manager", {})
    session, _ = broker.dispatch(
        state,
        "open",
        {
            "session": manager,
            "resource_name": dict(spec.resource_map)["psu"],
            "access_mode": 0,
            "open_timeout": 0,
        },
    )
    broker.dispatch(state, "close", {"session": session})
    broker.dispatch(state, "close", {"session": manager})
    broker.disconnect(state)
    bench.force_safe()
    bench.close()
    return journal.events


class ProjectionTests(unittest.TestCase):
    def test_projects_real_open_attributes_commands_responses_and_close(self) -> None:
        _, raw, _, _, _ = raw_nominal()
        projected = project_events(raw)
        self.assertTrue(projected)
        self.assertTrue(all(event.source_sequence is not None for event in projected))
        self.assertTrue(
            all(
                event.sequence // 10 == event.source_sequence
                for event in projected
            )
        )
        configured = {
            event.role
            for event in projected
            if event.operation
            in {"set_timeout", "set_read_termination", "set_write_termination"}
        }
        self.assertEqual(configured, {"psu", "switch", "awg", "scope", "dmm"})
        for operation in ("set_read_termination", "set_write_termination"):
            self.assertTrue(
                all(
                    base64.b64decode(event.request_b64) == b"\n"
                    for event in projected
                    if event.operation == operation
                    and event.request_b64 is not None
                )
            )
        commands = {
            base64.b64decode(event.request_b64)
            for event in projected
            if event.operation == "write" and event.request_b64 is not None
        }
        self.assertIn(b"*IDN?\n", commands)
        self.assertIn(b"CURVE?\n", commands)
        output_on = next(
            event
            for event in projected
            if event.operation == "write"
            and event.role == "awg"
            and event.state_after["awg_output"]
        )
        self.assertIsNotNone(output_on.state_after["stimulus_started_ms"])
        self.assertTrue(
            all(
                event.cleanup_source == "candidate"
                for event in projected
                if event.operation == "close"
            )
        )
        self.assertIn("force_safe", {event.operation for event in projected})

    def test_forced_close_and_protocol_reject_are_distinct(self) -> None:
        _, raw, _, _, _ = raw_nominal(force_scope_close=True, reject=True)
        projected = project_events(raw)
        forced = [
            event
            for event in projected
            if event.operation == "close" and event.cleanup_source == "forced"
        ]
        self.assertEqual([event.role for event in forced], ["scope"])
        rejects = [event for event in projected if event.operation == "protocol_reject"]
        self.assertEqual(len(rejects), 1)
        self.assertEqual(rejects[0].outcome, "error")
        self.assertEqual(
            len([event for event in projected if event.operation == "rpc_reject"]),
            2,
        )

    def test_transient_write_error_and_retry_preserve_raw_command(self) -> None:
        _, raw, _, _, _ = raw_nominal(transient=True)
        projected = project_events(raw)
        dmm_idn = [
            event
            for event in projected
            if event.operation == "write"
            and event.role == "dmm"
            and event.request_b64 is not None
            and base64.b64decode(event.request_b64) == b"*IDN?\n"
        ]
        self.assertEqual([event.outcome for event in dmm_idn], ["error", "ok"])
        self.assertIsNotNone(dmm_idn[0].error_code)
        self.assertNotIn(
            "protocol_reject", {event.operation for event in projected}
        )

    def test_many_leaks_do_not_cross_projected_sequence_buckets(self) -> None:
        _, raw, _, _, _ = raw_nominal(extra_scope_leaks=25)
        projected = project_events(raw)
        self.assertEqual(len({event.sequence for event in projected}), len(projected))
        forced_scope = [
            event
            for event in projected
            if event.operation == "close"
            and event.cleanup_source == "forced"
            and event.role == "scope"
        ]
        self.assertEqual(len(forced_scope), 1)

    def test_open_events_use_real_dirty_initial_state(self) -> None:
        projected = project_events(raw_dirty_initial_state())
        opened = next(
            event for event in projected if event.operation == "open_resource"
        )
        self.assertTrue(opened.state_before["psu_output"])
        self.assertTrue(opened.state_before["awg_output"])
        self.assertEqual(opened.state_before["closed_routes"], ("1199",))


if __name__ == "__main__":
    unittest.main()
