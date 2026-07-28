from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from instrument_benchmark_evaluator.container import docker_client
from instrument_benchmark_evaluator.container.docker_client import (
    DockerClient,
    DockerCommandResult,
)
from instrument_benchmark_evaluator.container.errors import (
    ContainerInfrastructureError,
)


class RecordingExecutor:
    def __init__(self, result=None, error=None) -> None:
        self.result = result or DockerCommandResult(0, "[]", "")
        self.error = error
        self.calls = []

    def __call__(self, argv, *, timeout, max_output_bytes):
        self.calls.append(
            {
                "argv": list(argv),
                "timeout": timeout,
                "max_output_bytes": max_output_bytes,
            }
        )
        if self.error:
            raise self.error
        return self.result


class DockerClientTests(unittest.TestCase):
    def test_attached_client_is_detached_before_container_kill(self) -> None:
        process = mock.Mock()
        events: list[str] = []
        process.terminate.side_effect = lambda: events.append("detach")
        process.wait.side_effect = lambda timeout: events.append("wait")
        with mock.patch.object(
            docker_client,
            "_kill_container",
            side_effect=lambda executable, container_id: events.append("kill"),
        ):
            docker_client._detach_and_kill(process, "docker", "container-1")
        self.assertEqual(events, ["detach", "wait", "kill"])

    def test_inspect_uses_argument_vector_without_shell(self) -> None:
        executor = RecordingExecutor(
            DockerCommandResult(0, '[{"Id":"abc"}]', "")
        )
        value = DockerClient(executor=executor).inspect("abc")
        self.assertEqual(value["Id"], "abc")
        self.assertEqual(executor.calls[0]["argv"], ["docker", "inspect", "abc"])

    def test_image_inspect_and_remove_use_exact_commands(self) -> None:
        executor = RecordingExecutor(
            DockerCommandResult(0, '[{"Id":"sha256:1"}]', "")
        )
        client = DockerClient(executor=executor)
        client.image_inspect("iab/test:v1")
        executor.result = DockerCommandResult(0, "abc\n", "")
        client.remove("abc")
        self.assertEqual(
            executor.calls[0]["argv"],
            ["docker", "image", "inspect", "iab/test:v1"],
        )
        self.assertEqual(
            executor.calls[1]["argv"],
            ["docker", "rm", "--force", "abc"],
        )

    def test_nonzero_command_is_typed_infrastructure_failure(self) -> None:
        executor = RecordingExecutor(
            DockerCommandResult(1, "", "daemon unavailable")
        )
        with self.assertRaisesRegex(
            ContainerInfrastructureError, "daemon unavailable"
        ):
            DockerClient(executor=executor).inspect("abc")

    def test_malformed_or_empty_inspect_is_rejected(self) -> None:
        for output in ("not-json", "[]", '{"Id":"abc"}'):
            with self.subTest(output=output):
                executor = RecordingExecutor(DockerCommandResult(0, output, ""))
                with self.assertRaisesRegex(
                    ContainerInfrastructureError, "inspect"
                ):
                    DockerClient(executor=executor).inspect("abc")

    def test_missing_binary_and_timeout_are_typed(self) -> None:
        cases = (
            (FileNotFoundError("docker"), "not found"),
            (subprocess.TimeoutExpired(["docker", "info"], 1), "timed out"),
        )
        for error, message in cases:
            with self.subTest(error=error):
                executor = RecordingExecutor(error=error)
                with self.assertRaisesRegex(ContainerInfrastructureError, message):
                    DockerClient(executor=executor).run(["info"], timeout=1)


if __name__ == "__main__":
    unittest.main()
