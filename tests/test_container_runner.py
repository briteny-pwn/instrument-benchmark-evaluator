from __future__ import annotations

import json
import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from instrument_benchmark_evaluator.container.contracts import (
    effective_policy,
    load_container_contract,
)
from instrument_benchmark_evaluator.container.docker_client import (
    AttachedContainerResult,
    DockerCommandResult,
)
from instrument_benchmark_evaluator.container.errors import ContainerInfrastructureError
from instrument_benchmark_evaluator.container.runner import run_container


ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT / "tests" / "fixtures" / "instance"


def inspect(exit_code: int = 0, *, oom: bool = False) -> dict:
    return {
        "Id": "container-1",
        "Image": "sha256:" + "1" * 64,
        "Created": "2026-07-24T00:00:00Z",
        "Config": {"User": "10001:10001", "StopTimeout": 1},
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 536870912,
            "NanoCpus": 1000000000,
            "PidsLimit": 64,
            "MemorySwap": 536870912,
            "LogConfig": {"Type": "none"},
            "Ulimits": [{"Name": "nofile", "Soft": 256, "Hard": 256}],
            "Tmpfs": {
                "/tmp": "rw,noexec,nosuid,nodev,size=64m",
                "/output": (
                    "rw,nosuid,nodev,noexec,uid=10001,gid=10001,"
                    "mode=0770,size=4194304"
                ),
            },
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
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/host/workspace",
                "Destination": "/workspace",
                "Mode": "ro",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": "/host/runner",
                "Destination": "/runner",
                "Mode": "ro",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": "/host/gateway",
                "Destination": "/run/iab",
                "Mode": "ro",
                "RW": False,
            },
        ],
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
        if argv[0] == "exec":
            payload = b'{"ok":true}'
            item = {
                "payload": base64.b64encode(payload).decode(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "uid": 10001,
                "gid": 10001,
                "mode": 0o644,
            }
            return DockerCommandResult(
                0,
                json.dumps({"result.json": item, "return.json": item}),
                "",
            )
        return DockerCommandResult(0, "", "")

    def start_attached(
        self,
        container_id,
        *,
        timeout,
        stdout_limit,
        stderr_limit,
        artifact_callback=None,
    ):
        self.calls.append(["start_attached", container_id])
        completed = self.exit_code == 0 and not self.timeout
        if completed and artifact_callback is not None:
            artifact_callback()
        return AttachedContainerResult(
            returncode=self.exit_code,
            stdout="stdout",
            stderr="stderr",
            timed_out=self.timeout,
            output_limited=False,
            completed_signal=completed,
        )

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

    def invoke(
        self,
        client: FakeClient,
        *,
        visa_socket_env: bool = False,
        fibsem_mode: bool = False,
    ):
        client.inspect_value["Image"] = self.contract.lock.image_digest
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
            visa_socket_env=visa_socket_env,
            fibsem_mode=fibsem_mode,
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
            "--log-driver=none",
            "--memory-swap=512m",
            "--ulimit=nofile=256:256",
            "--stop-timeout=1",
        ):
            self.assertIn(flag, create)
        self.assertTrue(any(item.startswith("--tmpfs=/output:") for item in create))
        self.assertNotIn("--privileged", create)
        self.assertNotIn("/var/run/docker.sock", " ".join(create))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.result, {"ok": True})
        self.assertTrue(result.container_evidence.cleanup_succeeded)

    def test_crash_oom_timeout_invalid_result_and_remove_failure(self) -> None:
        cases = (
            (FakeClient(exit_code=1), "candidate_failure"),
            (FakeClient(exit_code=137, oom=True), "candidate_oom"),
            (FakeClient(timeout=True), "candidate_timeout"),
            (FakeClient(exit_code=3), "invalid_result"),
            (FakeClient(exit_code=1, remove_failure=True), "infrastructure_failure"),
        )
        for client, status in cases:
            with self.subTest(status=status, remove=client.remove_failure):
                result = self.invoke(client)
                self.assertEqual(result.status, status)
                if client.timeout:
                    self.assertTrue(
                        any(call[0] == "start_attached" for call in client.calls)
                    )
                if client.remove_failure:
                    self.assertFalse(result.container_evidence.cleanup_succeeded)
                    self.assertEqual(result.candidate_status, "candidate_failure")

    def test_v2_adds_only_explicit_iab_socket_environment(self) -> None:
        v1 = FakeClient()
        self.invoke(v1)
        v2 = FakeClient()
        self.invoke(v2, visa_socket_env=True)
        v1_environment = [item for item in v1.calls[0] if item.startswith("--env=")]
        v2_environment = [item for item in v2.calls[0] if item.startswith("--env=")]
        self.assertEqual(v1_environment, ["--env=IAB_CONTAINER_MODE=1"])
        self.assertEqual(
            v2_environment,
            [
                "--env=IAB_CONTAINER_MODE=1",
                "--env=IAB_VISA_SOCKET=/run/iab/visa.sock",
            ],
        )
        self.assertNotIn("PYVISA_LIBRARY", " ".join(v2.calls[0]))
        transport = next(
            item for item in v2.calls[0] if "dst=/run/iab" in item
        )
        self.assertTrue(transport.endswith(",readonly"))

    def test_fibsem_adds_only_explicit_bootstrap_mode_environment(self) -> None:
        client = FakeClient()
        self.invoke(client, fibsem_mode=True)

        environment = [
            item for item in client.calls[0] if item.startswith("--env=")
        ]
        self.assertEqual(
            environment,
            ["--env=IAB_CONTAINER_MODE=1", "--env=IAB_FIBSEM_MODE=1"],
        )
        self.assertNotIn("IAB_VISA_SOCKET", " ".join(client.calls[0]))


if __name__ == "__main__":
    unittest.main()
