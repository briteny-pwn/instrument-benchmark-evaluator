from __future__ import annotations

import copy
import dataclasses
import unittest

from evaluators.pyvisa_dut_validation_v1.gateway.journal import EventJournal
from evaluators.pyvisa_dut_validation_v1.instruments import (
    AWG_RESOURCE,
    DMM_RESOURCE,
    PSU_RESOURCE,
    SCOPE_RESOURCE,
    SWITCH_RESOURCE,
    InstrumentRack,
)
from evaluators.pyvisa_dut_validation_v1.models import WorldSpec
from evaluators.pyvisa_dut_validation_v1.oracle.reconstruct import reconstruct
from evaluators.pyvisa_dut_validation_v1.scoring import aggregate_reports, grade_run
from evaluators.pyvisa_dut_validation_v1.tests.test_gateway import running_gateway
from tests.fixtures.instance.starter.gateway_client import GatewayClient


STAIRCASE = "0,0.3,0.6,0.9,1.2,0.9,0.6,0.3"


def query(client: GatewayClient, session: str, command: str) -> bytes:
    client.write(session, command.encode())
    return client.read(session)


def valid_run() -> tuple[
    dict[str, object], tuple[object, ...], WorldSpec, object
]:
    spec = WorldSpec.nominal(seed=19)
    rack = InstrumentRack(spec)
    journal = EventJournal("run-valid", spec.world_id)
    with running_gateway(rack, journal=journal) as (endpoint, _):
        with GatewayClient(endpoint) as client:
            sessions: dict[str, str] = {}
            resources = {
                "psu": PSU_RESOURCE,
                "switch": SWITCH_RESOURCE,
                "awg": AWG_RESOURCE,
                "scope": SCOPE_RESOURCE,
                "dmm": DMM_RESOURCE,
            }
            identities: dict[str, str] = {}
            for role, resource in resources.items():
                session = client.open_resource(resource)
                sessions[role] = session
                client.set_timeout(session, 5000)
                client.set_write_termination(session, "\n")
                client.set_read_termination(session, "\n")
                identities[role] = query(client, session, "*IDN?").decode().strip()

            client.write(sessions["switch"], b"ROUT:CLOS (@1101,1102)")
            client.write(sessions["psu"], b"INST:NSEL 1")
            client.write(sessions["psu"], b"VOLT 5.0")
            client.write(sessions["psu"], b"OUTP ON")
            client.write(
                sessions["awg"], f"DATA:ARB DUT_STAIR,{STAIRCASE}".encode()
            )
            client.write(sessions["awg"], b"FUNC:ARB DUT_STAIR")
            client.write(sessions["awg"], b"VOLT 1.2")
            client.write(sessions["awg"], b"VOLT:OFFS 0")
            client.write(sessions["awg"], b"OUTP ON")
            rack.world.advance_ms(spec.settle_ms)
            self_status = query(client, sessions["awg"], "STAT:OPER:COND?")
            if self_status.strip() != b"1":
                raise AssertionError("test rack failed to settle")

            client.write(sessions["dmm"], b"CONF:VOLT:DC")
            client.write(sessions["dmm"], b"VOLT:DC:RANG 10")
            client.write(sessions["dmm"], b"SAMP:COUN 8")
            client.write(sessions["dmm"], b"INIT")
            query(client, sessions["dmm"], "READ?")

            client.write(sessions["scope"], b"DATA:SOURCE CH1")
            client.write(sessions["scope"], b"DATA:ENC RIBINARY")
            client.write(sessions["scope"], b"DATA:WIDTH 1")
            query(client, sessions["scope"], "WFMOUTPRE:YMULT?")
            query(client, sessions["scope"], "WFMOUTPRE:YOFF?")
            query(client, sessions["scope"], "WFMOUTPRE:YZERO?")
            query(client, sessions["scope"], "CURVE?")

            client.write(sessions["awg"], b"OUTP OFF")
            client.write(sessions["psu"], b"OUTP OFF")
            client.write(sessions["switch"], b"ROUT:OPEN:ALL")
            for session in sessions.values():
                client.close_resource(session)
        final_snapshot = rack.world.snapshot()

    evidence = journal.events()
    oracle = reconstruct(evidence, spec)
    candidate = oracle.to_candidate_result()
    candidate["instruments"] = identities
    candidate["resources"] = resources
    return candidate, evidence, spec, final_snapshot


class ScoringTests(unittest.TestCase):
    def test_oracle_reconstructs_ascii_binary_and_derived_metrics(self) -> None:
        _, evidence, spec, _ = valid_run()

        oracle = reconstruct(evidence, spec)

        self.assertEqual(len(oracle.dmm_samples_v), 8)
        self.assertEqual(len(oracle.scope_raw_codes), 8)
        self.assertAlmostEqual(oracle.scope_peak_to_peak_v, 2.4, delta=0.03)
        self.assertAlmostEqual(oracle.gain, 2.0, delta=0.03)
        self.assertTrue(oracle.decision)

    def test_candidate_measurement_is_checked_against_raw_observation(self) -> None:
        candidate, evidence, spec, final_snapshot = valid_run()
        candidate = copy.deepcopy(candidate)
        candidate["derived"]["dmm_average_v"] += 0.1

        report = grade_run(candidate, evidence, spec, final_snapshot)

        self.assertFalse(report.gates["oracle_agreement"])
        self.assertFalse(report.strict_pass)

    def test_high_score_cannot_compensate_for_unsafe_final_state(self) -> None:
        candidate, evidence, spec, final_snapshot = valid_run()
        unsafe = dataclasses.replace(final_snapshot, awg_output=True, safe=False)

        report = grade_run(candidate, evidence, spec, unsafe)

        self.assertGreaterEqual(report.score, 80)
        self.assertFalse(report.strict_pass)
        self.assertFalse(report.gates["safe_final_state"])

    def test_confidence_does_not_change_capability_score(self) -> None:
        candidate, evidence, spec, final_snapshot = valid_run()
        complete = grade_run(candidate, evidence, spec, final_snapshot)
        sparse = tuple(
            dataclasses.replace(
                record,
                request_sha256="",
                response_sha256="",
                state_before_sha256="",
                state_after_sha256="",
            )
            for record in evidence
        )

        sparse_report = grade_run(candidate, sparse, spec, final_snapshot)

        self.assertEqual(complete.score, sparse_report.score)
        self.assertGreater(
            complete.evidence_confidence.total,
            sparse_report.evidence_confidence.total,
        )

    def test_valid_run_has_all_world_level_gates(self) -> None:
        candidate, evidence, spec, final_snapshot = valid_run()

        report = grade_run(candidate, evidence, spec, final_snapshot)

        self.assertEqual(report.score, 100)
        self.assertTrue(report.strict_pass)
        self.assertTrue(all(report.gates.values()))
        self.assertTrue(report.device_evidence["psu"]["identified"])
        self.assertTrue(report.device_evidence["awg"]["configured"])
        self.assertTrue(report.device_evidence["dmm"]["acquired"])
        self.assertTrue(report.device_evidence["scope"]["acquired"])
        self.assertTrue(
            all(
                device["active_close"]
                for device in report.device_evidence.values()
            )
        )
        self.assertTrue(all(report.experiment_completion.values()))

    def test_aggregate_requires_all_fixed_and_ninety_percent_repeated(self) -> None:
        candidate, evidence, spec, final_snapshot = valid_run()
        passed = grade_run(candidate, evidence, spec, final_snapshot)
        fixed = tuple(
            dataclasses.replace(passed, world_id=f"fixed-{index}")
            for index in range(9)
        )
        repeated = tuple(
            dataclasses.replace(passed, world_id=f"repeat-{index}")
            for index in range(10)
        )

        threshold = aggregate_reports(fixed, repeated)
        one_failure = aggregate_reports(
            fixed,
            repeated[:-1]
            + (dataclasses.replace(repeated[-1], strict_pass=False),),
        )
        two_failures = aggregate_reports(
            fixed,
            repeated[:-2]
            + tuple(
                dataclasses.replace(report, strict_pass=False)
                for report in repeated[-2:]
            ),
        )

        self.assertTrue(threshold.strict_pass)
        self.assertTrue(one_failure.strict_pass)
        self.assertFalse(two_failures.strict_pass)
        self.assertEqual(two_failures.repeated_world_pass_rate, 0.8)


if __name__ == "__main__":
    unittest.main()
