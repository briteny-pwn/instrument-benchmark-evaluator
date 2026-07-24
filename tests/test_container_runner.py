from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from instrument_benchmark_evaluator.container.contracts import (
    effective_policy,
    load_container_contract,
)
from instrument_benchmark_evaluator.container.docker_client import DockerCommandResult
from instrument_benchmark_evaluator.container.errors import (
    ContainerCommandTimeout,
    ContainerInfrastructureError,
)
from instrument_benchmark_evaluator.container.runner import run_container


ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT / "tests" / "fixtures" / "instance"


def inspect(exit_code: int = 0, *, oom: bool = False) -> dict:
    return {
        "Id": "container-1",
        "Image": "sha256:" + "1" * 64,
        "Created": "2026-07-24T00:00:00Z",
        "Config": {"User": "10001:10001"},
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 536870912,
            "NanoCpus": 1000000000,
            "PidsLimit": 64,
            "PidMode": "",
            "IpcMode": "private",
            "UTSMode": "",
        },
        "State": {
            "Status": "exited",
            "ExitCode": exit_code,
            "OOMKilled": oom,
            "StartedAt": "2026-07-24T00:00:01Z",
            "FinishedAt": "2026-07-24T00:00:02Z",
        },
        "Mounts": [],
    }


class FakeClient:
    def __init__(
        self,
        *,
        exit_code: int = 0,
        oom: bool = False,
        timeout: bool = False,
        remove_failure: bool = False,
    ) -> None:
        self.exit_code = exit_code
        self.inspect_value = inspect(exit_code, oom=oom)
        self.timeout = timeout
        self.remove_failure = remove_failure
        self.calls: list[list[str]] = []

    def run(self, arguments, *, timeout=None, check=True):
        argv = list(arguments)
        self.calls.append(argv)
        if argv[0] == "create":
            return DockerCommandResult(0, "container-1\n", "")
        if argv[0] == "wait":
            if self.timeout:
                self.timeout = False
                raise ContainerCommandTimeout("timed out")
            return DockerCommandResult(0, f"{self.exit_code}\n", "")
        if argv[0] == "logs":
            return DockerCommandResult(0, "stdout", "stderr")
        return DockerCommandResult(0, "", "")

    def inspect(self, container_id):
        self.calls.append(["inspect", container_id])
        return self.inspect_value

    def remove(self, container_id):
        self.calls.append(["remove", container_id])
        if self.remove_failure:
            raise ContainerInfrastructureError("remove failed")
        return DockerCommandResult(0, "", "")


class ContainerRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_container_contract(INSTANCE)
        self.policy = effective_policy(self.contract)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "output"
        self.gateway = self.root / "gateway" / "gateway.sock"
        self.runner = self.root / "runner"
        self.workspace.mkdir()
        self.output.mkdir()
        self.gateway.parent.mkdir()
        self.runner.mkdir()
        (self.workspace / "solution.py").write_text("# candidate")
        (self.gateway).touch()

    def invoke(self, client: FakeClient):
        if client.exit_code == 0 and not client.timeout:
            value = {"ok": True}
            (self.output / "result.json").write_text(json.dumps(value))
            (self.output / "return.json").write_text(json.dumps(value))
        return run_container(
            contract=self.contract,
            policy=self.policy,
            image_digest=self.contract.lock.image_digest,
            workspace=self.workspace,
            output_dir=self.output,
            gateway_socket=self.gateway,
            runner_dir=self.runner,
            client=client,
            run_id="run-1",
            world_id="nominal",
        )

    def test_completed_uses_all_hardening_flags_and_cleans_up(self) -> None:
        client = FakeClient()
        result = self.invoke(client)
        create = client.calls[0]
        for flag in (
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
        ):
            self.assertIn(flag, create)
        self.assertNotIn("--privileged", create)
        self.assertNotIn("/var/run/docker.sock", " ".join(create))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.result, {"ok": True})
        self.assertTrue(result.container_evidence.cleanup_succeeded)

    def test_crash_oom_timeout_invalid_result_and_remove_failure(self) -> None:
        cases = (
            (FakeClient(exit_code=1), "candidate_failure"),
            (FakeClient(exit_code=137, oom=True), "oom_killed"),
            (FakeClient(timeout=True), "candidate_timeout"),
            (FakeClient(exit_code=3), "invalid_result"),
            (FakeClient(exit_code=1, remove_failure=True), "candidate_failure"),
        )
        for client, status in cases:
            with self.subTest(status=status, remove=client.remove_failure):
                result = self.invoke(client)
                self.assertEqual(result.status, status)
                if client.timeout:
                    self.assertTrue(any(call[0] == "kill" for call in client.calls))
                if client.remove_failure:
                    self.assertFalse(result.container_evidence.cleanup_succeeded)


if __name__ == "__main__":
    unittest.main()
