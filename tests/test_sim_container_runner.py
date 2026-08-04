from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from instrument_benchmark_evaluator.container import sim_runner
from instrument_benchmark_evaluator.container.docker_client import DockerCommandResult
from instrument_benchmark_evaluator.container.errors import (
    ContainerCommandTimeout,
    ContainerInfrastructureError,
)
from instrument_benchmark_evaluator.container.sim_runner import SimContainerRunner
from tests.test_container_runner import inspect
from tests.test_sim_evidence import write_valid


class FakeDockerClient:
    def __init__(
        self,
        *,
        exit_code: int = 0,
        oom: bool = False,
        signal_failure: bool = False,
        wait_failure: bool = False,
        inspect_failure: bool = False,
        remove_failure: bool = False,
    ) -> None:
        self.exit_code = exit_code
        self.signal_failure = signal_failure
        self.wait_failure = wait_failure
        self.inspect_failure = inspect_failure
        self.remove_failure = remove_failure
        self.calls: list[tuple[str, object]] = []
        self.inspect_value = inspect(exit_code, oom=oom)
        self.inspect_value["Id"] = "sim-container"
        self.inspect_value["Image"] = "sha256:" + "a" * 64
        self.inspect_value["Config"]["User"] = "11001:11001"
        self.inspect_value["HostConfig"]["Memory"] = 536870912
        self.inspect_value["HostConfig"]["MemorySwap"] = 536870912
        self.inspect_value["HostConfig"]["Tmpfs"] = {
            "/tmp": "rw,noexec,nosuid,nodev,size=64m,uid=11001,gid=11001"
        }
        self.inspect_value["Config"]["StopTimeout"] = 2
        self.inspect_value["Mounts"] = [
            {
                "Type": "bind",
                "Source": "/host/transport",
                "Destination": "/run/iab/transport",
                "Mode": "rw",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/host/evidence",
                "Destination": "/run/iab/evidence",
                "Mode": "rw",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/host/world.json",
                "Destination": "/run/iab/world.json",
                "Mode": "ro",
                "RW": False,
            },
        ]

    def run(self, arguments, **kwargs):
        self.calls.append(("run", list(arguments)))
        if arguments[0] == "create":
            for argument in arguments:
                if not argument.startswith("--mount=type=bind,src="):
                    continue
                fields = dict(
                    item.split("=", 1)
                    for item in argument.removeprefix("--mount=").split(",")
                    if "=" in item
                )
                for mount in self.inspect_value["Mounts"]:
                    if mount["Destination"] == fields["dst"]:
                        mount["Source"] = fields["src"]
            return DockerCommandResult(0, "sim-container\n", "")
        return DockerCommandResult(0, "", "")

    def start_detached(self, container_id):
        self.calls.append(("start_detached", container_id))

    def signal(self, container_id, signal_name="TERM"):
        self.calls.append(("signal", (container_id, signal_name)))
        if self.signal_failure:
            raise ContainerInfrastructureError("container is not running")

    def wait(self, container_id, timeout):
        self.calls.append(("wait", (container_id, timeout)))
        if self.wait_failure:
            raise ContainerCommandTimeout("wait timed out")
        return self.exit_code

    def inspect(self, container_id):
        self.calls.append(("inspect", container_id))
        if self.inspect_failure:
            raise ContainerInfrastructureError("inspect failed")
        return self.inspect_value

    def remove(self, container_id):
        self.calls.append(("remove", container_id))
        if self.remove_failure:
            raise ContainerInfrastructureError("remove failed")


class SimContainerRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.transport = self.root / "transport"
        self.evidence = self.root / "evidence"
        self.world = self.root / "world.json"
        self.world.write_text("{}")
        self.image = "sha256:" + "a" * 64

    def runner(self, client, readiness=lambda endpoint, timeout: True):
        return SimContainerRunner(
            client=client,
            evaluator_image_id=self.image,
            readiness_probe=readiness,
        )

    def test_start_uses_exact_hardened_sibling_arguments(self) -> None:
        client = FakeDockerClient()
        handle = self.runner(client).start(
            run_id="run",
            world_id="world",
            world_path=self.world,
            transport_dir=self.transport,
            evidence_dir=self.evidence,
        )
        create = client.calls[0][1]
        self.assertEqual(
            create,
            [
                "create",
                f"--name={handle.name}",
                "--label=iab.managed=true",
                "--label=iab.role=sim",
                "--label=iab.run=run",
                "--label=iab.world=world",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--log-driver=none",
                "--user=11001:11001",
                "--cpus=1.0",
                "--memory=512m",
                "--memory-swap=512m",
                "--pids-limit=64",
                "--ulimit=nofile=256:256",
                "--stop-timeout=2",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,uid=11001,gid=11001",
                (
                    f"--mount=type=bind,src={self.transport.resolve()},"
                    "dst=/run/iab/transport"
                ),
                (
                    f"--mount=type=bind,src={self.evidence.resolve()},"
                    "dst=/run/iab/evidence"
                ),
                (
                    f"--mount=type=bind,src={self.world.resolve()},"
                    "dst=/run/iab/world.json,readonly"
                ),
                self.image,
                "serve-sim",
                "--world",
                "/run/iab/world.json",
                "--endpoint",
                "/run/iab/transport/visa.sock",
                "--evidence",
                "/run/iab/evidence",
                "--run-id",
                "run",
            ],
        )
        joined = " ".join(create)
        self.assertNotIn("docker.sock", joined)
        self.assertEqual(handle.container_id, "sim-container")

    def test_readiness_probe_receives_host_socket_and_timeout(self) -> None:
        calls = []

        def readiness(endpoint, timeout):
            calls.append((endpoint, timeout))
            return True

        self.runner(FakeDockerClient(), readiness=readiness).start(
            run_id="run",
            world_id="world",
            world_path=self.world,
            transport_dir=self.transport,
            evidence_dir=self.evidence,
        )
        self.assertEqual(calls, [(self.transport / "visa.sock", 10.0)])

    def test_default_readiness_probe_sends_exact_hello(self) -> None:
        calls = []

        class FakeRpcClient:
            def __init__(self, endpoint, *, socket_timeout):
                calls.append(("connect", endpoint, socket_timeout))

            def call(self, operation, arguments):
                calls.append(("call", operation, arguments))
                return tuple(sorted(sim_runner.OPERATIONS)), None

            def close(self):
                calls.append(("close",))

        endpoint = Path("/run/host/visa.sock")
        with patch.object(sim_runner, "RpcClient", FakeRpcClient):
            self.assertTrue(sim_runner._probe_readiness(endpoint, 0.1))
        self.assertEqual(calls[0][:2], ("connect", str(endpoint)))
        self.assertGreater(calls[0][2], 0)
        self.assertLessEqual(calls[0][2], 0.1)
        self.assertEqual(
            calls[1:],
            [
                ("call", "hello", {}),
                ("close",),
            ],
        )

    def test_finalize_signals_waits_inspects_verifies_then_removes(self) -> None:
        client = FakeDockerClient()
        runner = self.runner(client)
        handle = runner.start(
            run_id="run",
            world_id="world",
            world_path=self.world,
            transport_dir=self.transport,
            evidence_dir=self.evidence,
        )
        write_valid(self.evidence)
        result = runner.finalize(handle)
        lifecycle = [name for name, _ in client.calls]
        self.assertLess(lifecycle.index("signal"), lifecycle.index("wait"))
        self.assertLess(lifecycle.index("wait"), lifecycle.index("inspect"))
        self.assertLess(lifecycle.index("inspect"), lifecycle.index("remove"))
        self.assertTrue(result.journal_evidence.post_cleanup_snapshot["safe"])
        self.assertTrue(result.container_evidence.cleanup_succeeded)

    def test_start_timeout_and_finalize_failures_are_infrastructure_errors(
        self,
    ) -> None:
        with self.assertRaisesRegex(ContainerInfrastructureError, "readiness"):
            self.runner(
                FakeDockerClient(), readiness=lambda endpoint, timeout: False
            ).start(
                run_id="run", world_id="world", world_path=self.world,
                transport_dir=self.transport, evidence_dir=self.evidence,
            )
        transport = self.root / "transport-fatal"
        evidence = self.root / "evidence-fatal"
        client = FakeDockerClient(exit_code=70)
        runner = self.runner(client)
        handle = runner.start(
            run_id="run", world_id="world", world_path=self.world,
            transport_dir=transport, evidence_dir=evidence,
        )
        (evidence / "fatal.json").write_text(
            '{"schema_version":1,"run_id":"run","failure_kind":"trusted_sim_failure",'
            '"exception_type":"RuntimeError","message":"trusted simulator failed"}'
        )
        result = runner.finalize(handle)
        self.assertIsNotNone(result.fatal)

    def test_remove_failure_overrides_other_results(self) -> None:
        client = FakeDockerClient(remove_failure=True)
        runner = self.runner(client)
        handle = runner.start(
            run_id="run", world_id="world", world_path=self.world,
            transport_dir=self.transport, evidence_dir=self.evidence,
        )
        write_valid(self.evidence)
        with self.assertRaisesRegex(ContainerInfrastructureError, "remove"):
            runner.finalize(handle)

    def test_early_exit_nonzero_oom_wait_and_inspect_failures_remove(self) -> None:
        early = FakeDockerClient(exit_code=70)
        with self.assertRaisesRegex(ContainerInfrastructureError, "before readiness"):
            self.runner(early, readiness=lambda endpoint, timeout: False).start(
                run_id="run",
                world_id="world",
                world_path=self.world,
                transport_dir=self.root / "early-transport",
                evidence_dir=self.root / "early-evidence",
            )
        self.assertEqual([name for name, _ in early.calls][-2:], ["inspect", "remove"])

        cases = (
            (FakeDockerClient(exit_code=1), "exited with code 1"),
            (FakeDockerClient(oom=True), "OOM"),
            (FakeDockerClient(wait_failure=True), "wait timed out"),
            (FakeDockerClient(inspect_failure=True), "inspect failed"),
        )
        for index, (client, message) in enumerate(cases):
            with self.subTest(message=message):
                transport = self.root / f"transport-{index}"
                evidence = self.root / f"evidence-{index}"
                runner = self.runner(client)
                handle = runner.start(
                    run_id="run",
                    world_id="world",
                    world_path=self.world,
                    transport_dir=transport,
                    evidence_dir=evidence,
                )
                write_valid(evidence)
                with self.assertRaisesRegex(
                    (ContainerCommandTimeout, ContainerInfrastructureError), message
                ):
                    runner.finalize(handle)
                self.assertEqual(client.calls[-1][0], "remove")
                if client.wait_failure:
                    lifecycle = [name for name, _ in client.calls]
                    self.assertLess(lifecycle.index("wait"), lifecycle.index("inspect"))
                    self.assertLess(
                        lifecycle.index("inspect"), lifecycle.index("remove")
                    )

    def test_already_exited_sim_still_collects_fatal_evidence(self) -> None:
        client = FakeDockerClient(exit_code=70, signal_failure=True)
        runner = self.runner(client)
        handle = runner.start(
            run_id="run",
            world_id="world",
            world_path=self.world,
            transport_dir=self.transport,
            evidence_dir=self.evidence,
        )
        (self.evidence / "fatal.json").write_text(
            '{"schema_version":1,"run_id":"run",'
            '"failure_kind":"trusted_sim_failure",'
            '"exception_type":"RuntimeError",'
            '"message":"trusted simulator failed"}'
        )
        result = runner.finalize(handle)
        self.assertIsNotNone(result.fatal)
        lifecycle = [name for name, _ in client.calls]
        self.assertNotIn("wait", lifecycle)
        self.assertLess(lifecycle.index("signal"), lifecycle.index("inspect"))
        self.assertLess(lifecycle.index("inspect"), lifecycle.index("remove"))

    def test_rejects_symlink_world_before_creating_directories(self) -> None:
        link = self.root / "world-link.json"
        link.symlink_to(self.world)
        with self.assertRaisesRegex(ContainerInfrastructureError, "world"):
            self.runner(FakeDockerClient()).start(
                run_id="run",
                world_id="world",
                world_path=link,
                transport_dir=self.transport,
                evidence_dir=self.evidence,
            )
        self.assertFalse(self.transport.exists())

    def test_start_cleanup_failure_is_not_hidden(self) -> None:
        client = FakeDockerClient(remove_failure=True)
        with self.assertRaisesRegex(ContainerInfrastructureError, "remove failed"):
            self.runner(client, readiness=lambda endpoint, timeout: False).start(
                run_id="run",
                world_id="world",
                world_path=self.world,
                transport_dir=self.transport,
                evidence_dir=self.evidence,
            )


if __name__ == "__main__":
    unittest.main()
