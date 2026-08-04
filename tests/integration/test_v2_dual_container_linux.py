from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from evaluators.pyvisa_dut_validation_v1.worlds import load_world_specs
from instrument_benchmark_evaluator.candidate_backend import DockerCandidateBackend
from instrument_benchmark_evaluator.container.docker_client import DockerClient
from instrument_benchmark_evaluator.container.sim_runner import SimContainerRunner
from instrument_benchmark_evaluator.contracts import (
    RunSettings,
    load_instance_settings,
)
from instrument_benchmark_evaluator.v2_run import run_v2_world


ROOT = Path(__file__).resolve().parents[2]
INSTANCE = Path(
    os.environ.get(
        "IAB_INSTANCE_V2_ROOT",
        ROOT.parent / "instance" / "pyvisa_dut_validation_v2",
    )
)
INSTRUMENT = Path(
    os.environ.get("IAB_INSTRUMENT_ROOT", ROOT.parent / "instrument")
)
EVALUATOR = ROOT / "evaluators" / "pyvisa_dut_validation_v2"
WORLDS = ROOT / "evaluators" / "pyvisa_dut_validation_v1" / "worlds"
REFERENCE = EVALUATOR / "reference" / "solution.py"
BAD_PROTOCOL = EVALUATOR / "negatives" / "bad_protocol.py"
LEAKED_SESSIONS = EVALUATOR / "negatives" / "leaked_sessions.py"


class RecordingDockerClient(DockerClient):
    def __init__(self) -> None:
        super().__init__()
        self.removals: list[str] = []

    def remove(self, container_id: str):
        self.removals.append(container_id)
        return super().remove(container_id)


@unittest.skipUnless(
    os.environ.get("IAB_RUN_DOCKER_TESTS") == "1"
    and sys.platform.startswith("linux"),
    "v2 dual-container acceptance requires native Linux Docker",
)
class V2DualContainerLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from instrument_benchmark.evaluator_image import EvaluatorImageBuilder

        cls.builder = EvaluatorImageBuilder(
            assets_root=INSTRUMENT / "container"
        )
        cls.evaluator_image = cls.builder.build(
            ROOT, run_id="evaluator-v2-linux"
        )
        cls.addClassCleanup(cls.builder.remove, cls.evaluator_image)
        cls.instance = load_instance_settings(
            INSTANCE, expected_evaluator_id="pyvisa_dut_validation_v2"
        )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="iab-v2-linux-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = RecordingDockerClient()
        self.backend = DockerCandidateBackend.from_instance(
            self.instance,
            client=self.client,
            shared_run_root=self.root,
        )
        self.sim_runner = SimContainerRunner(
            client=self.client,
            evaluator_image_id=self.evaluator_image.image_id,
        )
        self.settings = RunSettings(
            instance_path=INSTANCE,
            fixed_worlds=("nominal",),
            repeated_worlds=10,
            timeout_seconds=30,
            max_output_bytes=1_048_576,
            run_id="v2-dual-linux",
            shared_run_root=self.root,
        )
        self.spec = load_world_specs(WORLDS)["nominal"]

    def run_candidate(self, candidate: Path):
        before = len(self.client.removals)
        execution = run_v2_world(
            benchmark=self.settings,
            instance=self.instance,
            spec=self.spec,
            candidate_path=candidate,
            backend=self.backend,
            sim_runner=self.sim_runner,
        )
        removals = self.client.removals[before:]
        self.assertIsNotNone(execution.process)
        self.assertIsNotNone(execution.sim_result)
        assert execution.process is not None and execution.sim_result is not None
        candidate_evidence = execution.process.container_evidence
        sim_evidence = execution.sim_result.container_evidence
        self.assertEqual(
            removals,
            [candidate_evidence.container_id, sim_evidence.container_id],
        )
        self.assertNotEqual(
            candidate_evidence.container_id, sim_evidence.container_id
        )
        self.assertEqual(
            candidate_evidence.image_digest,
            self.instance.container.lock.image_digest,
        )
        self.assertEqual(
            sim_evidence.image_digest, self.evaluator_image.image_id
        )
        self.assertEqual(candidate_evidence.user, "10001:10001")
        self.assertEqual(sim_evidence.user, "11001:11001")
        for evidence in (candidate_evidence, sim_evidence):
            self.assertEqual(evidence.network_mode, "none")
            self.assertTrue(evidence.readonly_rootfs)
            self.assertIn("ALL", evidence.cap_drop)
            self.assertIn("no-new-privileges", evidence.security_options)
            self.assertTrue(evidence.cleanup_attempted)
            self.assertTrue(evidence.cleanup_succeeded)
            self.assertNotIn(
                "/var/run/docker.sock",
                {mount.destination for mount in evidence.mounts},
            )
        candidate_mounts = {
            mount.destination: mount.writable
            for mount in candidate_evidence.mounts
        }
        sim_mounts = {
            mount.destination: mount.writable for mount in sim_evidence.mounts
        }
        self.assertEqual(
            candidate_mounts,
            {"/workspace": False, "/runner": False, "/run/iab": False},
        )
        self.assertEqual(
            sim_mounts,
            {
                "/run/iab/transport": True,
                "/run/iab/evidence": True,
                "/run/iab/world.json": False,
            },
        )
        self.assertNotIn("/workspace", sim_mounts)
        journal = execution.sim_result.journal_evidence
        self.assertEqual(journal.event_count, len(journal.events))
        self.assertEqual(journal.final_hash, journal.events[-1]["event_hash"])
        self.assertEqual(journal.events[0]["kind"], "lifecycle.start")
        self.assertEqual(journal.events[-1]["kind"], "lifecycle.exit")
        self.assertIn(
            "lifecycle.finalized",
            {event["kind"] for event in journal.events},
        )
        self.assertTrue(journal.post_cleanup_snapshot["safe"])
        return execution

    def test_reference_and_adversarial_candidates_preserve_isolation(self) -> None:
        executions = [
            self.run_candidate(candidate)
            for candidate in (REFERENCE, BAD_PROTOCOL, LEAKED_SESSIONS)
        ]
        reference, bad_protocol, leaked_sessions = executions
        self.assertTrue(reference.report.base.strict_pass)
        for execution in (bad_protocol, leaked_sessions):
            self.assertTrue(execution.report.base.infrastructure_valid)
            self.assertFalse(execution.report.base.retry_eligible)
            self.assertFalse(execution.report.base.strict_pass)
        self.assertFalse(
            bad_protocol.report.base.gates["no_forbidden_access"]
        )
        self.assertFalse(
            leaked_sessions.report.base.gates["active_close_all"]
        )
        all_ids = {
            evidence["container_id"]
            for execution in executions
            for evidence in (
                execution.report.candidate_container_evidence,
                execution.report.sim_container_evidence,
            )
        }
        self.assertEqual(len(all_ids), 6)

    def test_candidate_cannot_read_hidden_files_or_replace_socket(self) -> None:
        probe = self.root / "probe.py"
        probe.write_text(
            """from __future__ import annotations
import json
import os
import pyvisa

def run_experiment(instrument_endpoint: str, output_path: str) -> dict:
    del instrument_endpoint
    with pyvisa.ResourceManager("@iab") as manager:
        resources = list(manager.list_resources())
    blocked = {}
    for path in ("/run/iab/world.json", "/run/iab/evidence"):
        try:
            open(path, "rb").read(1)
            blocked[path] = False
        except OSError:
            blocked[path] = True
    try:
        os.unlink("/run/iab/visa.sock")
        blocked["unlink_socket"] = False
    except OSError:
        blocked["unlink_socket"] = True
    result = {
        "resources": resources,
        "blocked": blocked,
        "docker_socket": os.path.exists("/var/run/docker.sock"),
    }
    with open(output_path, "w", encoding="utf-8") as stream:
        json.dump(result, stream)
    return result
""",
            encoding="utf-8",
        )
        execution = self.run_candidate(probe)
        assert execution.process is not None
        result = execution.process.result
        self.assertEqual(len(result["resources"]), 5)
        self.assertTrue(all(result["blocked"].values()))
        self.assertFalse(result["docker_socket"])


if __name__ == "__main__":
    unittest.main()
