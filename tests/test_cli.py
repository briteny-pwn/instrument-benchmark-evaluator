from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from instrument_benchmark_evaluator.contracts import (
    ContractError,
    EvaluatorRequest,
    load_evaluator_request,
)
from instrument_benchmark_evaluator.cli import main


ROOT = Path(__file__).resolve().parents[1]


class EvaluatorCliContractTests(unittest.TestCase):
    def valid_request(self, directory: Path) -> dict[str, object]:
        instance = directory / "instance"
        candidate = directory / "solution.py"
        instance.mkdir()
        candidate.write_text("def run_experiment(endpoint, output): pass\n")
        return {
            "protocol_version": 1,
            "run_id": "contract-test",
            "instance_id": "pyvisa_dut_validation_v1",
            "instance_path": str(instance),
            "candidate_path": str(candidate),
            "timeout_seconds": 30,
            "max_output_bytes": 65536,
            "repeated_worlds": 10,
            "repeated_base_seed": 40000,
        }

    def test_load_request_resolves_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "request.json"
            path.write_text(json.dumps(self.valid_request(root)))
            request = load_evaluator_request(path)
            self.assertIsInstance(request, EvaluatorRequest)
            self.assertTrue(request.instance_path.is_absolute())
            self.assertTrue(request.candidate_path.is_absolute())

    def test_request_rejects_wrong_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = self.valid_request(root)
            value["protocol_version"] = 2
            path = root / "request.json"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "protocol_version"):
                load_evaluator_request(path)

    def test_request_rejects_wrong_instance_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = self.valid_request(root)
            value["instance_id"] = "other"
            path = root / "request.json"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "instance_id"):
                load_evaluator_request(path)

    def test_cli_runs_reference_candidate_and_writes_report(self) -> None:
        instance = ROOT / "tests" / "fixtures" / "instance"
        candidate = (
            ROOT
            / "evaluators"
            / "pyvisa_dut_validation_v1"
            / "reference"
            / "solution.py"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_path = root / "request.json"
            report_path = root / "report.json"
            value = self.valid_request(root)
            value.update(
                {
                    "instance_path": str(instance),
                    "candidate_path": str(candidate),
                    "repeated_worlds": 1,
                }
            )
            request_path.write_text(json.dumps(value))
            status = main(
                [
                    "run",
                    "--request",
                    str(request_path),
                    "--report",
                    str(report_path),
                ]
            )
            self.assertEqual(status, 0)
            report = json.loads(report_path.read_text())
            self.assertTrue(report["strict_pass"])
            self.assertEqual(report["evaluator"]["protocol_version"], 1)
            self.assertEqual(len(report["worlds"]), 10)


if __name__ == "__main__":
    unittest.main()
