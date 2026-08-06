from __future__ import annotations

import os
import unittest
from pathlib import Path

from instrument_benchmark_evaluator.container.contracts import (
    load_container_contract,
)
from instrument_benchmark_evaluator.container.docker_client import DockerClient
from instrument_benchmark_evaluator.container.image import build_image


ROOT = Path(__file__).resolve().parents[2]
_default_instance = ROOT.parent / "instance" / "sources" / "pyvisa" / "pyvisa_dut_validation_v1"
_worktree_instance = (
    ROOT.parent / "instance-docker-runner" / "pyvisa_dut_validation_v1"
)
REAL_INSTANCE = Path(
    os.environ.get(
        "IAB_INSTANCE_ROOT",
        _default_instance if _default_instance.is_dir() else _worktree_instance,
    )
)


@unittest.skipUnless(
    os.environ.get("IAB_RUN_DOCKER_TESTS") == "1",
    "set IAB_RUN_DOCKER_TESTS=1 for required Docker integration",
)
class LinuxContainerImageTests(unittest.TestCase):
    def test_real_instance_image_matches_lock(self) -> None:
        contract = load_container_contract(REAL_INSTANCE)
        evidence = build_image(
            contract,
            DockerClient(),
            instance_id="pyvisa_dut_validation_v1",
        )
        self.assertEqual(evidence.platform, "linux/amd64")
        self.assertEqual(evidence.user, "10001:10001")
        self.assertEqual(evidence.image_id, contract.lock.image_digest)


if __name__ == "__main__":
    unittest.main()
