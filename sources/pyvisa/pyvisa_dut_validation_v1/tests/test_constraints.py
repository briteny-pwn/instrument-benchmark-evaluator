from __future__ import annotations

import unittest
from dataclasses import replace

from sources.pyvisa.pyvisa_dut_validation_v1.constraints.partial_order import (
    SemanticEvent,
    evaluate_constraints,
    normalize_events,
)
from sources.pyvisa.pyvisa_dut_validation_v1.gateway.journal import EventJournal
from sources.pyvisa.pyvisa_dut_validation_v1.models import WorldSpec
from sources.pyvisa.pyvisa_dut_validation_v1.tests.test_gateway import running_gateway
from sources.pyvisa.pyvisa_dut_validation_v1.tests.test_instruments import ready_rack
from tests.fixtures.instance.starter.gateway_client import (
    GatewayClient,
    GatewayError,
)


ROLES = ("switch", "psu", "awg", "dmm", "scope")


def event(
    sequence: int,
    kind: str,
    *,
    role: str | None = None,
    session: str | None = None,
    cleanup_source: str | None = None,
) -> SemanticEvent:
    return SemanticEvent(
        sequence=sequence,
        kind=kind,
        role=role,
        session_digest=session,
        cleanup_source=cleanup_source,
    )


def valid_events(acquisition_order: tuple[str, str]) -> tuple[SemanticEvent, ...]:
    events: list[SemanticEvent] = []
    sequence = 1
    sessions: dict[str, str] = {}
    for role in ROLES:
        sessions[role] = f"session-{role}"
        events.append(event(sequence, "resource.opened", role=role, session=sessions[role]))
        sequence += 1
        events.append(event(sequence, "device.identified", role=role, session=sessions[role]))
        sequence += 1
    for kind, role in (
        ("switch.routes_closed", "switch"),
        ("psu.configured", "psu"),
        ("psu.output_on", "psu"),
        ("awg.waveform_uploaded", "awg"),
        ("awg.waveform_selected", "awg"),
        ("awg.configured", "awg"),
        ("awg.output_on", "awg"),
        ("dut.settled", "awg"),
    ):
        events.append(event(sequence, kind, role=role, session=sessions[role]))
        sequence += 1
    for role in acquisition_order:
        events.append(
            event(
                sequence,
                f"{role}.acquired",
                role=role,
                session=sessions[role],
            )
        )
        sequence += 1
    for kind, role in (
        ("awg.output_off", "awg"),
        ("psu.output_off", "psu"),
        ("switch.routes_opened", "switch"),
    ):
        events.append(event(sequence, kind, role=role, session=sessions[role]))
        sequence += 1
    for role in ROLES:
        events.append(
            event(
                sequence,
                "resource.closed",
                role=role,
                session=sessions[role],
                cleanup_source="candidate",
            )
        )
        sequence += 1
    return tuple(events)


class ConstraintTests(unittest.TestCase):
    def test_valid_dmm_scope_orderings_both_pass(self) -> None:
        for order in (("dmm", "scope"), ("scope", "dmm")):
            results = evaluate_constraints(valid_events(order), final_state_safe=True)
            self.assertTrue(
                all(result.passed for result in results),
                [(result.name, result.message) for result in results if not result.passed],
            )

    def test_acquisition_before_settle_fails_named_constraint(self) -> None:
        events = list(valid_events(("dmm", "scope")))
        settle = next(item for item in events if item.kind == "dut.settled")
        dmm = next(item for item in events if item.kind == "dmm.acquired")
        events[events.index(settle)], events[events.index(dmm)] = (
            event(settle.sequence, "dmm.acquired", role="dmm", session="session-dmm"),
            event(dmm.sequence, "dut.settled", role="awg", session="session-awg"),
        )

        results = evaluate_constraints(events, final_state_safe=True)

        result = next(
            result for result in results if result.name == "settled_before_acquisition"
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.evidence_sequences)

    def test_forced_close_is_not_active_cleanup(self) -> None:
        events = list(valid_events(("dmm", "scope")))
        close = next(
            item
            for item in events
            if item.kind == "resource.closed" and item.role == "dmm"
        )
        events[events.index(close)] = event(
            close.sequence,
            close.kind,
            role=close.role,
            session=close.session_digest,
            cleanup_source="forced",
        )

        results = evaluate_constraints(events, final_state_safe=True)

        self.assertFalse(
            next(result for result in results if result.name == "active_close_all").passed
        )

    def test_unsafe_final_state_fails_independently_of_trace(self) -> None:
        results = evaluate_constraints(
            valid_events(("scope", "dmm")), final_state_safe=False
        )

        self.assertFalse(
            next(result for result in results if result.name == "safe_final_state").passed
        )

    def test_journal_records_query_bytes_hashes_state_and_explicit_close(self) -> None:
        rack = ready_rack()
        journal = EventJournal("run-1", "nominal")
        with running_gateway(rack, journal=journal) as (endpoint, _):
            with GatewayClient(endpoint) as client:
                session = client.open_resource(rack.list_resources()[0])
                client.write(session, b"*IDN?\n")
                client.read(session)
                client.close_resource(session)

        records = journal.events()
        self.assertEqual([record.sequence for record in records], list(range(1, len(records) + 1)))
        write = next(record for record in records if record.operation == "write")
        self.assertEqual(len(write.request_sha256), 64)
        self.assertEqual(len(write.state_before_sha256), 64)
        self.assertEqual(len(write.state_after_sha256), 64)
        close = next(record for record in records if record.operation == "close")
        self.assertEqual(close.cleanup_source, "candidate")
        semantic = normalize_events(records)
        self.assertIn("device.identified", {item.kind for item in semantic})

    def test_transient_instrument_error_and_recovery_are_both_journaled(self) -> None:
        spec = replace(
            WorldSpec.nominal(71),
            world_id="command_error",
            transient_error_role="dmm",
            transient_error_command="*IDN?",
            transient_error_count=1,
        )
        from sources.pyvisa.pyvisa_dut_validation_v1.instruments import (
            DMM_RESOURCE,
            InstrumentRack,
        )

        rack = InstrumentRack(spec)
        journal = EventJournal("run-error", spec.world_id)
        with running_gateway(rack, journal=journal) as (endpoint, _):
            with GatewayClient(endpoint) as client:
                session = client.open_resource(DMM_RESOURCE)
                with self.assertRaises(GatewayError):
                    client.write(session, b"*IDN?\n")
                client.write(session, b"*IDN?\n")
                client.read(session)
                client.close_resource(session)

        writes = [record for record in journal.events() if record.operation == "write"]
        self.assertEqual([record.outcome for record in writes], ["error", "ok"])
        self.assertEqual(writes[0].error_code, "instrument_error")
        identified = [
            item for item in normalize_events(journal.events())
            if item.kind == "device.identified"
        ]
        self.assertEqual(len(identified), 1)


if __name__ == "__main__":
    unittest.main()
