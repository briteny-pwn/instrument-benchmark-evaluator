from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from instrument_benchmark_evaluator.container.contracts import (
    effective_policy,
    load_container_contract,
)
from instrument_benchmark_evaluator.container.docker_client import DockerClient
from instrument_benchmark_evaluator.container.runner import run_container


ROOT = Path(__file__).resolve().parents[2]
_default_instance = ROOT.parent / "instance" / "pyvisa_dut_validation_v1"
_worktree_instance = (
    ROOT.parent / "instance-docker-runner" / "pyvisa_dut_validation_v1"
)
INSTANCE = Path(
    os.environ.get(
        "IAB_INSTANCE_ROOT",
        _default_instance if _default_instance.is_dir() else _worktree_instance,
    )
)
CANDIDATES = ROOT / "tests" / "fixtures" / "candidates"
RUNNER = ROOT / "instrument_benchmark_evaluator"


@unittest.skipUnless(
    os.environ.get("IAB_RUN_DOCKER_TESTS") == "1",
    "set IAB_RUN_DOCKER_TESTS=1 for required Docker integration",
)
class LinuxContainerIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_container_contract(INSTANCE)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "output"
        self.gateway_dir = self.root / "gateway"
        self.workspace.mkdir(mode=0o755)
        self.output.mkdir(mode=0o777)
        self.output.chmod(0o777)
        self.gateway_dir.mkdir(mode=0o755)
        self.gateway = self.gateway_dir / "gateway.sock"

    def _serve_once(self) -> threading.Thread:
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(self.gateway))
        self.gateway.chmod(0o777)
        server.listen(1)

        def serve() -> None:
            try:
                connection, _ = server.accept()
                with connection:
                    if connection.recv(4) == b"PING":
                        connection.sendall(b"PONG")
            finally:
                server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread

    def _run(self, candidate: str, *, serve: bool = False):
        shutil.copy2(CANDIDATES / candidate, self.workspace / "solution.py")
        thread = self._serve_once() if serve else None
        result = run_container(
            contract=self.contract,
            policy=effective_policy(self.contract),
            image_digest=self.contract.lock.image_digest,
            workspace=self.workspace,
            output_dir=self.output,
            gateway_socket=self.gateway,
            runner_dir=RUNNER,
            client=DockerClient(),
            run_id="isolation",
            world_id=Path(candidate).stem,
            expected_output_uid=10001,
        )
        if thread:
            thread.join(timeout=3)
        return result

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "host Unix socket bind mounts require a native Linux Docker host",
    )
    def test_network_filesystem_identity_and_gateway_boundary(self) -> None:
        result = self._run("probe_isolation.py", serve=True)
        self.assertEqual(result.status, "completed", result.stderr)
        self.assertEqual(result.result["uid"], 10001)
        self.assertTrue(result.result["gateway_ok"])
        self.assertTrue(
            all(
                not probe["succeeded"]
                for probe in result.result["probes"].values()
            )
        )
        evidence = result.container_evidence
        self.assertEqual(evidence.network_mode, "none")
        self.assertTrue(evidence.readonly_rootfs)
        self.assertIn("ALL", evidence.cap_drop)
        self.assertIn("no-new-privileges", evidence.security_options)

    def test_memory_limit_is_observed_as_oom(self) -> None:
        result = self._run("fill_memory.py")
        self.assertEqual(result.status, "candidate_oom")
        self.assertTrue(result.container_evidence.oom_killed)

    def test_bounded_tmpfs_result_is_collected(self) -> None:
        result = self._run("simple_result.py")
        self.assertEqual(result.status, "completed", result.stderr)
        self.assertEqual(result.result, {"ok": True})
        self.assertEqual(result.artifact_evidence.uid, 10001)

    def test_pid_limit_contains_process_pressure(self) -> None:
        result = self._run("fork_bomb_guarded.py")
        self.assertEqual(result.status, "candidate_failure")
        self.assertEqual(result.container_evidence.pids_limit, 64)

    def test_stdout_limit_kills_candidate_immediately(self) -> None:
        result = self._run("flood_stdout.py")
        self.assertEqual(result.status, "output_limit")

    def test_output_tmpfs_contains_disk_pressure(self) -> None:
        result = self._run("fill_output.py")
        self.assertEqual(result.status, "candidate_failure")


if __name__ == "__main__":
    unittest.main()
