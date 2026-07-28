from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

import yaml

from instrument_benchmark_evaluator.contracts import (
    RunSettings,
    load_instance_settings,
)
from instrument_benchmark_evaluator.isolation import IsolationError, prepare_workspace
from instrument_benchmark_evaluator.run import run_world
from instrument_benchmark_evaluator.host_submission import HostCandidateBackend
from evaluators.pyvisa_dut_validation_v1.worlds import (
    load_world_specs,
    repeated_specs,
)


ROOT = Path(__file__).resolve().parents[3]
EVALUATOR_ROOT = ROOT / "evaluators" / "pyvisa_dut_validation_v1"
REFERENCE = EVALUATOR_ROOT / "reference" / "solution.py"
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


def settings() -> tuple[RunSettings, object]:
    return (
        RunSettings(
            instance_path=INSTANCE_ROOT,
            fixed_worlds=FIXED_WORLDS,
            repeated_worlds=10,
            timeout_seconds=30,
            max_output_bytes=1_048_576,
        ),
        load_instance_settings(INSTANCE_ROOT),
    )


class HiddenWorldTests(unittest.TestCase):
    def test_all_nine_required_world_families_are_configured(self) -> None:
        specs = load_world_specs(EVALUATOR_ROOT / "worlds")

        self.assertEqual(set(specs), set(FIXED_WORLDS))
        self.assertEqual(len(specs), 9)

    def test_worlds_change_the_declared_dimensions(self) -> None:
        specs = load_world_specs(EVALUATOR_ROOT / "worlds")
        nominal = specs["nominal"]

        self.assertNotEqual(
            specs["reordered_resources"].resource_map, nominal.resource_map
        )
        self.assertGreaterEqual(len(specs["distractor_devices"].distractors), 1)
        self.assertLessEqual(len(specs["distractor_devices"].distractors), 3)
        self.assertEqual(specs["numeric_formats"].dmm_format, "scientific")
        self.assertNotEqual(
            specs["binary_block_variants"].binary_length_digits,
            nominal.binary_length_digits,
        )
        self.assertGreater(
            specs["delayed_settle"].settle_ms, nominal.settle_ms
        )
        self.assertTrue(specs["dirty_initial_state"].initial_psu_output)
        self.assertLess(specs["dut_gain_failure"].gain, nominal.gain_min)
        self.assertEqual(specs["command_error"].transient_error_role, "dmm")

    def test_repeated_specs_are_deterministic_and_parameterized(self) -> None:
        first = repeated_specs(10, base_seed=5000)
        second = repeated_specs(10, base_seed=5000)

        self.assertEqual(first, second)
        self.assertEqual(len({spec.seed for spec in first}), 10)
        self.assertGreater(len({spec.resource_map for spec in first}), 1)
        self.assertEqual(
            {spec.dmm_format for spec in first}, {"decimal", "scientific"}
        )
        self.assertEqual(
            {spec.binary_length_digits for spec in first}, {1, 2, 3}
        )

    def test_reference_solution_strict_passes_every_fixed_world(self) -> None:
        benchmark, instance = settings()
        specs = load_world_specs(EVALUATOR_ROOT / "worlds")

        for world_id in benchmark.fixed_worlds:
            execution = run_world(
                benchmark=benchmark,
                instance=instance,
                spec=specs[world_id],
                candidate_path=REFERENCE,
                backend=HostCandidateBackend(),
            )
            self.assertEqual(execution.process.status, "completed", world_id)
            self.assertTrue(
                execution.report.strict_pass,
                (world_id, execution.report.to_dict(), execution.process.stderr),
            )
            self.assertEqual(execution.report.score, 100, world_id)

    def test_declared_negative_submissions_fail_intended_checks(self) -> None:
        benchmark, instance = settings()
        specs = load_world_specs(EVALUATOR_ROOT / "worlds")
        matrix = yaml.safe_load(
            (EVALUATOR_ROOT / "adversarial_matrix.yaml").read_text(encoding="utf-8")
        )

        for case in matrix["cases"]:
            temporary: tempfile.TemporaryDirectory[str] | None = None
            if case.get("base") == "reference":
                temporary = tempfile.TemporaryDirectory()
                candidate = Path(temporary.name) / case["submission"]
                source = REFERENCE.read_text(encoding="utf-8")
                for replacement in case["replacements"]:
                    old = replacement["old"]
                    if old not in source:
                        self.fail(
                            f"{case['submission']} mutation source is absent: {old!r}"
                        )
                    source = source.replace(old, replacement["new"])
                candidate.write_text(source, encoding="utf-8")
            else:
                candidate = EVALUATOR_ROOT / "negatives" / case["submission"]
            if case["expected_status"] == "invalid_submission":
                with self.subTest(case=case["submission"]):
                    with self.assertRaises(IsolationError, msg=case["submission"]):
                        with tempfile.TemporaryDirectory() as directory:
                            prepare_workspace(
                                benchmark.instance_path,
                                candidate,
                                instance,
                                Path(directory) / "workspace",
                            )
                if temporary is not None:
                    temporary.cleanup()
                continue
            try:
                execution = run_world(
                    benchmark=benchmark,
                    instance=instance,
                    spec=specs[case["world"]],
                    candidate_path=candidate,
                    backend=HostCandidateBackend(),
                )
                self.assertEqual(
                    execution.process.status,
                    case["expected_status"],
                    (case["submission"], execution.process.stderr),
                )
                for gate in case["failed_gates"]:
                    self.assertFalse(
                        execution.report.gates[gate],
                        (case["submission"], gate, execution.report.to_dict()),
                    )
            finally:
                if temporary is not None:
                    temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
