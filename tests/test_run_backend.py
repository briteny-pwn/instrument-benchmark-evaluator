from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sources.pyvisa.pyvisa_dut_validation_v1.instruments import PSU_RESOURCE
from sources.pyvisa.pyvisa_dut_validation_v1.worlds import load_world_specs
from instrument_benchmark_evaluator.contracts import (
    RunSettings,
    load_instance_settings,
)
from instrument_benchmark_evaluator.candidate_backend import DockerCandidateBackend
from instrument_benchmark_evaluator.container.image import ImageEvidence
from instrument_benchmark_evaluator.host_submission import ProcessResult
from instrument_benchmark_evaluator.run import run_world
from tests.fixtures.instance.starter.gateway_client import GatewayClient


ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT / "tests" / "fixtures" / "instance"
EVALUATOR = ROOT / "sources" / "pyvisa" / "pyvisa_dut_validation_v1"


class UnsafeFailureBackend:
    def invoke(self, *, endpoint, **kwargs):
        with GatewayClient(endpoint) as gateway:
            session = gateway.open_resource(PSU_RESOURCE)
            gateway.write(session, b"VOLT 5")
            gateway.write(session, b"OUTP ON")
        return ProcessResult("candidate_failure", 1, "", "boom", None)


class PathRecordingBackend:
    def __init__(self) -> None:
        self.workspace: Path | None = None
        self.endpoint: Path | None = None

    def invoke(self, *, workspace, endpoint, **kwargs):
        self.workspace = workspace
        self.endpoint = endpoint
        return ProcessResult("candidate_failure", 1, "", "boom", None)


class RunBackendTests(unittest.TestCase):
    def test_docker_backend_stages_bootstrap_below_shared_world_root(self) -> None:
        instance = load_instance_settings(INSTANCE)
        image = ImageEvidence(
            image_reference="candidate:test",
            image_id="sha256:" + "a" * 64,
            platform="linux/amd64",
            user="10001:10001",
            dockerfile_sha256="b" * 64,
            base_images=(),
            repo_digests=(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "image-only-runner"
            (source / "container").mkdir(parents=True)
            (source / "bootstrap.py").write_text("# bootstrap\n")
            (source / "container" / "bootstrap_contract.py").write_text(
                "# contract\n"
            )
            world = root / "shared" / "w-1"
            workspace = world / "workspace"
            workspace.mkdir(parents=True)
            endpoint = world / "gateway.sock"
            backend = DockerCandidateBackend(
                client=object(), image=image, runner_dir=source
            )
            with patch(
                "instrument_benchmark_evaluator.candidate_backend.run_container"
            ) as invoked:
                backend.invoke(
                    workspace=workspace,
                    candidate_path=workspace / "solution.py",
                    endpoint=endpoint,
                    instance=instance,
                    timeout_seconds=5,
                    max_output_bytes=65536,
                    run_id="run",
                    world_id="world",
                )
            staged = invoked.call_args.kwargs["runner_dir"]
            self.assertEqual(staged, (world / "runner").resolve())
            self.assertEqual((staged / "bootstrap.py").read_text(), "# bootstrap\n")
            self.assertTrue((staged / "container" / "bootstrap_contract.py").is_file())

    def test_world_paths_are_created_below_shared_run_root(self) -> None:
        instance = load_instance_settings(INSTANCE)
        candidate = EVALUATOR / "reference" / "solution.py"
        spec = load_world_specs(EVALUATOR / "worlds")["nominal"]
        backend = PathRecordingBackend()
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "shared"
            shared.mkdir()
            benchmark = RunSettings(
                instance_path=INSTANCE,
                fixed_worlds=("nominal",),
                repeated_worlds=1,
                timeout_seconds=5,
                max_output_bytes=65536,
                shared_run_root=shared,
            )
            run_world(
                benchmark=benchmark,
                instance=instance,
                spec=spec,
                candidate_path=candidate,
                backend=backend,
            )
            assert backend.workspace is not None
            assert backend.endpoint is not None
            self.assertTrue(backend.workspace.is_relative_to(shared))
            self.assertTrue(backend.endpoint.is_relative_to(shared))
            self.assertFalse(backend.workspace.exists())

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
