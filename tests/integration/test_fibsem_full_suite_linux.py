from __future__ import annotations

import json
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
from instrument_benchmark_evaluator.fibsem_run import run_fibsem_full_suite


ROOT = Path(__file__).resolve().parents[2]
SIBLINGS = ROOT.parent
INSTANCE = Path(
    os.environ.get("IAB_FIBSEM_INSTANCE_ROOT", SIBLINGS / "instance" / "fibsem_liftout_v1")
)
INSTRUMENT = Path(os.environ.get("IAB_INSTRUMENT_ROOT", SIBLINGS / "instrument"))
OPENFIBSEM = Path(os.environ.get("IAB_OPENFIBSEM_ROOT", SIBLINGS / "fibsem"))
REFERENCE = ROOT / "evaluators" / "fibsem_liftout_v1" / "reference" / "solution.py"
OPENFIBSEM_COMMIT = "2ebccb8b9721234ca66bb94de36d0f7cfe047af9"


def deterministic_projection(report) -> dict[str, object]:
    return {
        "score": report.score,
        "strict_pass": report.strict_pass,
        "strict_gates": dict(report.strict_gates),
        "worlds": [
            {
                "world_id": world.world_id,
                "score": world.score,
                "strict_pass": world.strict_pass,
                "step_scores": dict(world.step_scores),
                "partial_order": dict(world.partial_order),
                "scenario_digest": world.trusted_evidence["scenario_digest"],
                "geometry": {
                    step: checkpoint.geometry.canonical_geometry_hash
                    for step, checkpoint in world.checkpoints.items()
                },
            }
            for world in report.worlds
        ],
    }


def artifact_projection(root: Path) -> dict[str, dict[str, dict[str, str]]]:
    projection = {}
    for checkpoint in sorted(root.glob("fibsem-w-*/evidence/artifacts/*/step_*/checkpoint.json")):
        value = json.loads(checkpoint.read_text())
        projection.setdefault(value["world_id"], {})[value["step_id"]] = {
            "geometry": value["geometry_hash"],
            "sem": value["artifacts"]["sem.png"]["sha256"],
            "fib": value["artifacts"]["fib.png"]["sha256"],
        }
    return projection


@unittest.skipUnless(
    os.environ.get("IAB_RUN_FIBSEM_DOCKER_TESTS") == "1"
    and sys.platform.startswith("linux"),
    "FIBSEM full-suite acceptance requires native Linux Docker",
)
class FibsemFullSuiteLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(INSTRUMENT / "src"))
        from instrument_benchmark.evaluator_image import EvaluatorImageBuilder

        cls.builder = EvaluatorImageBuilder(assets_root=INSTRUMENT / "container")
        cls.evaluator_image = cls.builder.build(
            ROOT,
            run_id="fibsem-full-linux",
            openfibsem_checkout=OPENFIBSEM,
            openfibsem_commit=OPENFIBSEM_COMMIT,
        )
        cls.addClassCleanup(cls.builder.remove, cls.evaluator_image)
        cls.instance = load_instance_settings(
            INSTANCE, expected_evaluator_id="fibsem_liftout_v1"
        )

    def test_reference_full_suite_is_strict_and_deterministic(self) -> None:
        reports = []
        artifacts = []
        for index in range(2):
            temporary = tempfile.TemporaryDirectory(prefix=f"iab-fibsem-full-{index}-")
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
            client = DockerClient()
            backend = DockerCandidateBackend.from_instance(
                self.instance, client=client, shared_run_root=root
            )
            simulator = FibsemSimContainerRunner(
                client=client,
                evaluator_image_id=self.evaluator_image.image_id,
                readiness_timeout=60,
            )
            settings = RunSettings(
                instance_path=INSTANCE,
                fixed_worlds=("nominal", "small", "large", "needle_offset", "target_pose"),
                repeated_worlds=5,
                timeout_seconds=180,
                max_output_bytes=1_048_576,
                run_id="fibsem-full-linux",
                shared_run_root=root,
            )
            reports.append(
                run_fibsem_full_suite(
                    benchmark=settings,
                    instance=self.instance,
                    candidate_path=REFERENCE,
                    backend=backend,
                    sim_runner=simulator,
                    repeated_base_seed=47000,
                )
            )
            artifacts.append(artifact_projection(root))

        for report, projection in zip(reports, artifacts, strict=True):
            self.assertTrue(report.strict_pass)
            self.assertEqual(report.score, 100)
            self.assertEqual(len(report.worlds), 10)
            self.assertEqual(sum(len(world.checkpoints) for world in report.worlds), 40)
            self.assertEqual(sum(len(steps) for steps in projection.values()), 40)
        self.assertEqual(
            deterministic_projection(reports[0]),
            deterministic_projection(reports[1]),
        )
        self.assertEqual(artifacts[0], artifacts[1])


if __name__ == "__main__":
    unittest.main()
