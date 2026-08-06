from __future__ import annotations

import dataclasses
import unittest

from sources.pyvisa.pyvisa_dut_validation_v1.oracle.reconstruct import reconstruct
from sources.pyvisa.pyvisa_dut_validation_v1.scoring import (
    WEIGHTS,
    aggregate_reports,
    grade_run,
)
from sources.pyvisa.pyvisa_dut_validation_v2.projection import project_events
from sources.pyvisa.pyvisa_dut_validation_v2.reports import (
    V2EvaluationReport,
    V2WorldReport,
)
from sources.pyvisa.pyvisa_dut_validation_v2.tests.test_projection import raw_nominal


class V2ScoringTests(unittest.TestCase):
    def test_real_projected_nominal_evidence_scores_one_hundred(self) -> None:
        spec, raw, final_snapshot, identities, resources = raw_nominal()
        projected = project_events(raw)
        candidate = reconstruct(projected, spec).to_candidate_result()
        candidate["instruments"] = identities
        candidate["resources"] = resources
        report = grade_run(candidate, projected, spec, final_snapshot)
        self.assertEqual(
            WEIGHTS,
            {
                "discovery": 15,
                "driver": 15,
                "causal_state": 20,
                "experiment": 25,
                "safety": 10,
                "robustness": 15,
            },
        )
        self.assertEqual(report.score, 100)
        self.assertTrue(report.strict_pass)

    def test_schema_three_reports_rename_and_separate_all_evidence(self) -> None:
        spec, raw, final_snapshot, _, _ = raw_nominal()
        projected = project_events(raw)
        oracle = reconstruct(projected, spec)
        base_world = grade_run(
            oracle.to_candidate_result(), projected, spec, final_snapshot
        )
        candidate_evidence = {"role": "candidate"}
        base_world = dataclasses.replace(
            base_world, container_evidence=candidate_evidence
        )
        fixed = tuple(
            dataclasses.replace(base_world, world_id=f"fixed-{index}")
            for index in range(9)
        )
        repeated = tuple(
            dataclasses.replace(base_world, world_id=f"repeat-{index}")
            for index in range(10)
        )
        base = aggregate_reports(fixed, repeated)
        wrapped = tuple(
            V2WorldReport(
                report,
                candidate_container_evidence=candidate_evidence,
                sim_container_evidence={"role": "sim"},
                sim_journal_evidence={"event_count": len(raw)},
            )
            for report in fixed + repeated
        )
        report = V2EvaluationReport(base, wrapped).to_dict()
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["source_id"], "pyvisa")
        self.assertEqual(len(report["worlds"]), 19)
        world = report["worlds"][0]
        self.assertNotIn("container_evidence", world)
        self.assertEqual(world["candidate_container_evidence"]["role"], "candidate")
        self.assertEqual(world["sim_container_evidence"]["role"], "sim")
        self.assertEqual(world["sim_journal_evidence"]["event_count"], len(raw))

    def test_missing_evidence_requires_retryable_infrastructure_failure(self) -> None:
        spec, raw, final_snapshot, _, _ = raw_nominal()
        projected = project_events(raw)
        base = grade_run(None, projected, spec, final_snapshot)
        with self.assertRaises(ValueError):
            V2WorldReport(base, None, {"sim": True}, {"journal": True})
        retryable = dataclasses.replace(
            base, infrastructure_valid=False, retry_eligible=True
        )
        value = V2WorldReport(retryable, None, None, None).to_dict()
        self.assertFalse(value["infrastructure_valid"])
        self.assertTrue(value["retry_eligible"])


if __name__ == "__main__":
    unittest.main()
