from __future__ import annotations

import unittest
from pathlib import Path

from instrument_benchmark_evaluator.contracts import (
    RunSettings,
    load_instance_settings,
)
from instrument_benchmark_evaluator.run import run_full_suite


ROOT = Path(__file__).resolve().parents[3]
EVALUATOR = ROOT / "evaluators" / "pyvisa_dut_validation_v1"
REFERENCE = EVALUATOR / "reference" / "solution.py"
INSTANCE_ROOT = ROOT / "tests" / "fixtures" / "instance"
FIXED_WORLDS = (
    "nominal",
    "reordered_resources",
    "distractor_devices",
    "numeric_formats",
    "binary_block_variants",
    "delayed_settle",
    "dirty_initial_state",
    "dut_gain_failure",
    "command_error",
)


def semantic_projection(report: dict[str, object]) -> dict[str, object]:
    def project(value):
        if isinstance(value, dict):
            return {
                key: project(item)
                for key, item in value.items()
                if key != "evidence_sequences"
            }
        if isinstance(value, list):
            return [project(item) for item in value]
        return value

    return project(report)


class EndToEndTests(unittest.TestCase):
    def test_reference_full_suite_is_reproducible_and_strict_pass(self) -> None:
        benchmark = RunSettings(
            instance_path=INSTANCE_ROOT,
            fixed_worlds=FIXED_WORLDS,
            repeated_worlds=10,
            timeout_seconds=30,
            max_output_bytes=1_048_576,
        )
        instance = load_instance_settings(INSTANCE_ROOT)

        first = run_full_suite(
            benchmark=benchmark,
            instance=instance,
            candidate_path=REFERENCE,
            world_directory=EVALUATOR / "worlds",
            repeated_base_seed=30000,
        )
        second = run_full_suite(
            benchmark=benchmark,
            instance=instance,
            candidate_path=REFERENCE,
            world_directory=EVALUATOR / "worlds",
            repeated_base_seed=30000,
        )

        self.assertTrue(first.strict_pass)
        self.assertEqual(first.fixed_world_pass_rate, 1.0)
        self.assertGreaterEqual(first.repeated_world_pass_rate, 0.9)
        self.assertEqual(
            semantic_projection(first.to_dict()),
            semantic_projection(second.to_dict()),
        )
        self.assertTrue(all(first.strict_gates.values()))
        self.assertEqual(len(first.fixed_reports), 9)
        self.assertEqual(len(first.repeated_reports), 10)


if __name__ == "__main__":
    unittest.main()
