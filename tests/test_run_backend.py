from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evaluators.pyvisa_dut_validation_v1.instruments import PSU_RESOURCE
from evaluators.pyvisa_dut_validation_v1.worlds import load_world_specs
from instrument_benchmark_evaluator.contracts import (
    RunSettings,
    load_instance_settings,
)
from instrument_benchmark_evaluator.host_submission import ProcessResult
from instrument_benchmark_evaluator.run import run_world
from tests.fixtures.instance.starter.gateway_client import GatewayClient


ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT / "tests" / "fixtures" / "instance"
EVALUATOR = ROOT / "evaluators" / "pyvisa_dut_validation_v1"


class UnsafeFailureBackend:
    def invoke(self, *, endpoint, **kwargs):
        with GatewayClient(endpoint) as gateway:
            session = gateway.open_resource(PSU_RESOURCE)
            gateway.write(session, b"VOLT 5")
            gateway.write(session, b"OUTP ON")
        return ProcessResult("candidate_failure", 1, "", "boom", None)


class RunBackendTests(unittest.TestCase):
    def test_candidate_failure_captures_unsafe_state_then_forces_cleanup(self) -> None:
        instance = load_instance_settings(INSTANCE)
        benchmark = RunSettings(
            instance_path=INSTANCE,
            fixed_worlds=("nominal",),
            repeated_worlds=1,
            timeout_seconds=5,
            max_output_bytes=65536,
        )
        candidate = EVALUATOR / "reference" / "solution.py"
        spec = load_world_specs(EVALUATOR / "worlds")["nominal"]
        execution = run_world(
            benchmark=benchmark,
            instance=instance,
            spec=spec,
            candidate_path=candidate,
            backend=UnsafeFailureBackend(),
        )
        self.assertFalse(execution.pre_cleanup_snapshot.safe)
        self.assertTrue(execution.forced_cleanup)
        self.assertTrue(execution.post_cleanup_snapshot.safe)
        self.assertEqual(execution.report.status, "candidate_failure")


if __name__ == "__main__":
    unittest.main()
