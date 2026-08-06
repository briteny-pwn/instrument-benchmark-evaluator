from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from instrument_benchmark_evaluator.candidate_backend import DockerCandidateBackend
from instrument_benchmark_evaluator.contracts import (
    RunSettings,
    load_instance_settings,
)
from instrument_benchmark_evaluator.host_submission import HostCandidateBackend
from instrument_benchmark_evaluator.run import run_full_suite, run_world
from sources.pyvisa.pyvisa_dut_validation_v1.worlds import load_world_specs


ROOT = Path(__file__).resolve().parents[2]
_default_instance = ROOT.parent / "instance" / "sources" / "pyvisa" / "pyvisa_dut_validation_v1"
_worktree_instance = (
    ROOT.parent / "instance-docker-runner" / "pyvisa_dut_validation_v1"
)
INSTANCE = Path(
    os.environ.get(
        "IAB_INSTANCE_ROOT",
        _default_instance if _default_instance.is_dir() else _worktree_instance,
    )
)
EVALUATOR = ROOT / "sources" / "pyvisa" / "pyvisa_dut_validation_v1"
REFERENCE = EVALUATOR / "reference" / "solution.py"
FIXED = (
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


def semantic_projection(value):
    if isinstance(value, dict):
        ignored = {
            "container_evidence",
            "artifact_evidence",
            "container_runtime",
            "evidence_sequences",
            "total",
        }
        return {
            key: semantic_projection(item)
            for key, item in value.items()
            if key not in ignored
        }
    if isinstance(value, list):
        return [semantic_projection(item) for item in value]
    return value


@unittest.skipUnless(
    os.environ.get("IAB_RUN_DOCKER_TESTS") == "1"
    and sys.platform.startswith("linux"),
    "full Docker experiment suite requires a native Linux host",
)
class DockerFullSuiteLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.instance = load_instance_settings(
            INSTANCE,
            expected_source_id="pyvisa",
            expected_instance_id="pyvisa_dut_validation_v1",
            expected_evaluator_id="pyvisa_dut_validation_v1",
        )
        cls.docker = DockerCandidateBackend.from_instance(cls.instance)
        cls.settings = RunSettings(
            instance_path=INSTANCE,
            fixed_worlds=FIXED,
            repeated_worlds=10,
            timeout_seconds=30,
            max_output_bytes=1_048_576,
            run_id="docker-full-suite",
        )

    def test_nominal_semantics_match_host_backend(self) -> None:
        spec = load_world_specs(EVALUATOR / "worlds")["nominal"]
        host = run_world(
            benchmark=self.settings,
            instance=self.instance,
            spec=spec,
            candidate_path=REFERENCE,
            backend=HostCandidateBackend(),
        )
        docker = run_world(
            benchmark=self.settings,
            instance=self.instance,
            spec=spec,
            candidate_path=REFERENCE,
            backend=self.docker,
        )
        self.assertEqual(
            semantic_projection(host.report.to_dict()),
            semantic_projection(docker.report.to_dict()),
        )

    def test_full_nineteen_world_suite_is_reproducible(self) -> None:
        reports = [
            run_full_suite(
                benchmark=self.settings,
                instance=self.instance,
                candidate_path=REFERENCE,
                world_directory=EVALUATOR / "worlds",
                repeated_base_seed=30000,
                backend=self.docker,
            )
            for _ in range(2)
        ]
        for report in reports:
            worlds = report.fixed_reports + report.repeated_reports
            self.assertTrue(report.strict_pass)
            self.assertEqual(report.score, 100)
            self.assertEqual(report.fixed_world_pass_rate, 1.0)
            self.assertEqual(report.repeated_world_pass_rate, 1.0)
            self.assertEqual(len(worlds), 19)
            self.assertEqual(
                len(
                    {
                        world.container_evidence["container_id"]
                        for world in worlds
                    }
                ),
                19,
            )
        self.assertEqual(
            semantic_projection(reports[0].to_dict()),
            semantic_projection(reports[1].to_dict()),
        )


if __name__ == "__main__":
    unittest.main()
