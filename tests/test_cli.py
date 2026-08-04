from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from instrument_benchmark_evaluator.contracts import (
    ContractError,
    EvaluatorRequest,
    load_evaluator_request,
)
from instrument_benchmark_evaluator.cli import main
from instrument_benchmark_evaluator.host_submission import HostCandidateBackend
from instrument_benchmark_evaluator.container.errors import (
    ContainerInfrastructureError,
)


ROOT = Path(__file__).resolve().parents[1]


class EvaluatorCliContractTests(unittest.TestCase):
    def test_serve_sim_dispatches_absolute_paths_and_exact_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            world = root / "world.json"
            endpoint = root / "transport" / "visa.sock"
            evidence = root / "evidence"
            simulator = root / "simulator.yaml"
            with patch(
                "instrument_benchmark_evaluator.cli._serve_sim",
                return_value=17,
            ) as serve:
                status = main(
                    [
                        "serve-sim",
                        "--world",
                        str(world),
                        "--endpoint",
                        str(endpoint),
                        "--evidence",
                        str(evidence),
                        "--simulator",
                        str(simulator),
                        "--run-id",
                        "run-17",
                    ]
                )
            self.assertEqual(status, 17)
            serve.assert_called_once_with(
                [
                    "--world",
                    str(world.resolve()),
                    "--endpoint",
                    str(endpoint.resolve()),
                    "--evidence",
                    str(evidence.resolve()),
                    "--simulator",
                    str(simulator.resolve()),
                    "--run-id",
                    "run-17",
                ]
            )

    def test_serve_sim_uses_packaged_simulator_when_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "instrument_benchmark_evaluator.cli._serve_sim",
                return_value=0,
            ) as serve:
                status = main(
                    [
                        "serve-sim",
                        "--world",
                        str(root / "world.json"),
                        "--endpoint",
                        str(root / "transport" / "visa.sock"),
                        "--evidence",
                        str(root / "evidence"),
                        "--run-id",
                        "run-default",
                    ]
                )
            self.assertEqual(status, 0)
            forwarded = serve.call_args.args[0]
            self.assertNotIn("--simulator", forwarded)
            self.assertEqual(forwarded[-2:], ["--run-id", "run-default"])

    def test_packaged_evaluator_manifest_matches_repository_contract(self) -> None:
        self.assertEqual(
            (ROOT / "evaluator.yaml").read_bytes(),
            (ROOT / "instrument_benchmark_evaluator" / "evaluator.yaml").read_bytes(),
        )

    def valid_request(self, directory: Path) -> dict[str, object]:
        instance = directory / "instance"
        candidate = directory / "solution.py"
        shared_run_root = directory / "s"
        instance.mkdir()
        shared_run_root.mkdir()
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
            "container_protocol_version": 1,
            "image_mode": "locked",
            "shared_run_root": str(shared_run_root),
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
            self.assertEqual(
                request.shared_run_root,
                (root / "s").resolve(),
            )

    def test_v2_request_requires_exact_evaluator_image_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = self.valid_request(root)
            value["instance_id"] = "pyvisa_dut_validation_v2"
            value["evaluator_image_id"] = "sha256:" + "a" * 64
            path = root / "request.json"
            path.write_text(json.dumps(value))
            request = load_evaluator_request(path)
            self.assertEqual(request.evaluator_image_id, "sha256:" + "a" * 64)

            for invalid in (None, "sha256:" + "A" * 64, "sha256:1234"):
                with self.subTest(invalid=invalid):
                    changed = dict(value)
                    if invalid is None:
                        changed.pop("evaluator_image_id")
                    else:
                        changed["evaluator_image_id"] = invalid
                    path.write_text(json.dumps(changed))
                    with self.assertRaises(ContractError):
                        load_evaluator_request(path)

    def test_v1_request_rejects_v2_image_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = self.valid_request(root)
            value["evaluator_image_id"] = "sha256:" + "a" * 64
            path = root / "request.json"
            path.write_text(json.dumps(value))
            with self.assertRaises(ContractError):
                load_evaluator_request(path)

    def test_request_rejects_relative_shared_run_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = self.valid_request(root)
            value["shared_run_root"] = "shared-runs"
            path = root / "request.json"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "shared_run_root"):
                load_evaluator_request(path)

    def test_request_rejects_filesystem_root_as_shared_run_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = self.valid_request(root)
            value["shared_run_root"] = "/"
            path = root / "request.json"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "shared_run_root"):
                load_evaluator_request(path)

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

    def test_cli_defaults_to_docker_and_reports_infrastructure_failure(self) -> None:
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
            with patch(
                "instrument_benchmark_evaluator.cli."
                "DockerCandidateBackend.from_instance",
                side_effect=ContainerInfrastructureError("docker unavailable"),
            ) as construct:
                status = main(
                    [
                        "run",
                        "--request",
                        str(request_path),
                        "--report",
                        str(report_path),
                    ]
                )
            self.assertEqual(status, 3)
            construct.assert_called_once()
            self.assertEqual(
                construct.call_args.kwargs["shared_run_root"],
                Path(value["shared_run_root"]).resolve(),
            )
            self.assertFalse(report_path.exists())

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
                ],
                backend_factory=lambda instance: HostCandidateBackend(),
            )
            self.assertEqual(status, 0)
            report = json.loads(report_path.read_text())
            self.assertTrue(report["strict_pass"])
            self.assertEqual(report["evaluator"]["protocol_version"], 1)
            self.assertEqual(len(report["worlds"]), 10)

    def test_cli_dispatches_v2_with_exact_image_id_and_schema_two(self) -> None:
        instance = ROOT.parent / "instance" / "pyvisa_dut_validation_v2"
        candidate = (
            ROOT
            / "evaluators"
            / "pyvisa_dut_validation_v2"
            / "reference"
            / "solution.py"
        )

        class Report:
            def to_dict(self):
                return {"schema_version": 2, "strict_pass": True, "worlds": []}

        backend = object()
        sim_runner = object()
        image_id = "sha256:" + "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_path = root / "request.json"
            report_path = root / "report.json"
            value = self.valid_request(root)
            value.update(
                {
                    "instance_id": "pyvisa_dut_validation_v2",
                    "instance_path": str(instance),
                    "candidate_path": str(candidate),
                    "evaluator_image_id": image_id,
                }
            )
            request_path.write_text(json.dumps(value))
            with patch(
                "instrument_benchmark_evaluator.v2_run.run_v2_full_suite",
                return_value=Report(),
            ) as run:
                status = main(
                    [
                        "run",
                        "--request",
                        str(request_path),
                        "--report",
                        str(report_path),
                    ],
                    backend_factory=lambda settings: backend,
                    sim_runner_factory=lambda exact_image_id: (
                        self.assertEqual(exact_image_id, image_id) or sim_runner
                    ),
                )
            self.assertEqual(status, 0)
            self.assertIs(run.call_args.kwargs["backend"], backend)
            self.assertIs(run.call_args.kwargs["sim_runner"], sim_runner)
            report = json.loads(report_path.read_text())
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(
                report["evaluator"]["id"], "pyvisa_dut_validation_v2"
            )


if __name__ == "__main__":
    unittest.main()
