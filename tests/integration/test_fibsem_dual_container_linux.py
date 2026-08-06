from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from instrument_benchmark_evaluator.candidate_backend import DockerCandidateBackend
from instrument_benchmark_evaluator.container.docker_client import DockerClient
from instrument_benchmark_evaluator.container.fibsem_sim_runner import (
    FibsemSimContainerRunner,
)
from instrument_benchmark_evaluator.contracts import (
    RunSettings,
    load_instance_settings,
)
from instrument_benchmark_evaluator.fibsem_run import (
    fibsem_suite_specs,
    run_fibsem_world,
)


ROOT = Path(__file__).resolve().parents[2]
SIBLINGS = ROOT.parent
INSTANCE = Path(
    os.environ.get("IAB_FIBSEM_INSTANCE_ROOT", SIBLINGS / "instance" / "sources" / "openfibsem" / "fibsem_liftout_v1")
)
INSTRUMENT = Path(os.environ.get("IAB_INSTRUMENT_ROOT", SIBLINGS / "instrument"))
OPENFIBSEM = Path(os.environ.get("IAB_OPENFIBSEM_ROOT", SIBLINGS / "fibsem"))
EVALUATOR = ROOT / "sources" / "openfibsem" / "fibsem_liftout_v1"
REFERENCE = EVALUATOR / "reference" / "solution.py"
PRIVATE_IMPORT = EVALUATOR / "negatives" / "private_import.py"
FAKE_CHECKPOINT = EVALUATOR / "negatives" / "fake_checkpoint.py"
OPENFIBSEM_COMMIT = "2ebccb8b9721234ca66bb94de36d0f7cfe047af9"


@unittest.skipUnless(
    os.environ.get("IAB_RUN_FIBSEM_DOCKER_TESTS") == "1"
    and sys.platform.startswith("linux"),
    "FIBSEM dual-container acceptance requires native Linux Docker",
)
class FibsemDualContainerLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(INSTRUMENT / "src"))
        from instrument_benchmark.evaluator_image import EvaluatorImageBuilder

        cls.builder = EvaluatorImageBuilder(assets_root=INSTRUMENT / "container")
        cls.evaluator_image = cls.builder.build(
            ROOT,
            run_id="fibsem-dual-linux",
            openfibsem_checkout=OPENFIBSEM,
            openfibsem_commit=OPENFIBSEM_COMMIT,
        )
        cls.addClassCleanup(cls.builder.remove, cls.evaluator_image)
        cls.instance = load_instance_settings(
            INSTANCE,
            expected_source_id="openfibsem",
            expected_instance_id="fibsem_liftout_v1",
            expected_evaluator_id="fibsem_liftout_v1",
        )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="iab-fibsem-linux-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = DockerClient()
        self.backend = DockerCandidateBackend.from_instance(
            self.instance,
            client=self.client,
            shared_run_root=self.root,
        )
        self.sim_runner = FibsemSimContainerRunner(
            client=self.client,
            evaluator_image_id=self.evaluator_image.image_id,
            readiness_timeout=60,
        )

    def run_candidate(self, candidate: Path, *, timeout: float = 180):
        settings = RunSettings(
            instance_path=INSTANCE,
            fixed_worlds=("nominal", "small", "large", "needle_offset", "target_pose"),
            repeated_worlds=5,
            timeout_seconds=timeout,
            max_output_bytes=1_048_576,
            run_id=f"fibsem-{candidate.stem}-linux",
            shared_run_root=self.root,
        )
        spec = fibsem_suite_specs(INSTANCE, repeated_base_seed=47000)[0]
        return run_fibsem_world(
            benchmark=settings,
            instance=self.instance,
            spec=spec,
            candidate_path=candidate,
            backend=self.backend,
            sim_runner=self.sim_runner,
        )

    def test_reference_proves_isolation_and_four_trusted_checkpoints(self) -> None:
        execution = self.run_candidate(REFERENCE)
        self.assertTrue(execution.report.strict_pass)
        self.assertEqual(execution.report.score, 100)
        self.assertEqual(tuple(execution.report.checkpoints), (
            "step_1", "step_2", "step_3", "step_4"
        ))
        assert execution.process is not None and execution.sim_result is not None
        candidate = execution.process.container_evidence
        simulator = execution.sim_result.container_evidence
        self.assertEqual(candidate.user, "10001:10001")
        self.assertEqual(simulator.user, "11001:11001")
        self.assertNotEqual(candidate.container_id, simulator.container_id)
        for evidence in (candidate, simulator):
            self.assertEqual(evidence.network_mode, "none")
            self.assertTrue(evidence.readonly_rootfs)
            self.assertIn("ALL", evidence.cap_drop)
            self.assertIn("no-new-privileges", evidence.security_options)
            self.assertTrue(evidence.cleanup_succeeded)
        candidate_mounts = {mount.destination for mount in candidate.mounts}
        simulator_mounts = {mount.destination for mount in simulator.mounts}
        self.assertNotIn("/run/iab/world.json", candidate_mounts)
        self.assertNotIn("/run/iab/evidence", candidate_mounts)
        self.assertNotIn("/workspace", simulator_mounts)
        self.assertNotIn("/run/evaluator/request.json", simulator_mounts)
        assert execution.evidence_root is not None
        artifact_root = execution.evidence_root / "artifacts" / "nominal"
        self.assertEqual(
            {path.name for path in artifact_root.iterdir()},
            {"step_1", "step_2", "step_3", "step_4"},
        )

    def test_private_import_and_fake_checkpoint_do_not_pass(self) -> None:
        private = self.run_candidate(PRIVATE_IMPORT)
        forged = self.run_candidate(FAKE_CHECKPOINT)
        self.assertFalse(private.report.strict_pass)
        self.assertFalse(private.report.strict_gates["no_forbidden_access"])
        self.assertFalse(forged.report.strict_pass)
        self.assertFalse(forged.report.strict_gates["all_checkpoint_states"])
        self.assertTrue(private.report.terminal.is_safe)
        self.assertTrue(forged.report.terminal.is_safe)


if __name__ == "__main__":
    unittest.main()
