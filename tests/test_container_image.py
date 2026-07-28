from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from instrument_benchmark_evaluator.container.contracts import (
    load_container_contract,
)
from instrument_benchmark_evaluator.container.docker_client import (
    DockerCommandResult,
)
from instrument_benchmark_evaluator.container.errors import ImagePolicyError
from instrument_benchmark_evaluator.container.image import (
    build_image,
    resolve_image,
)


ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT / "tests" / "fixtures" / "instance"


class FakeDockerClient:
    def __init__(self, inspect_value: dict) -> None:
        self.inspect_value = inspect_value
        self.calls: list[list[str]] = []
        self.context_files: set[str] | None = None

    def image_inspect(self, image_ref: str) -> dict:
        self.calls.append(["image_inspect", image_ref])
        return self.inspect_value

    def run(self, arguments, *, timeout=None, check=True):
        self.calls.append(list(arguments))
        if arguments and arguments[0] == "buildx":
            self.context_files = {
                path.name for path in Path(arguments[-1]).iterdir()
            }
        return DockerCommandResult(0, "", "")


def valid_inspect(contract) -> dict:
    return {
        "Id": contract.lock.image_digest,
        "RepoTags": [contract.lock.image_reference],
        "RepoDigests": [],
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {
            "User": "10001:10001",
            "Labels": {
                "iab.instance": "pyvisa_dut_validation_v1",
                "iab.dockerfile-sha256": contract.lock.dockerfile_sha256,
            },
        },
    }


class ContainerImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_container_contract(INSTANCE)

    def test_resolve_image_normalizes_verified_evidence(self) -> None:
        client = FakeDockerClient(valid_inspect(self.contract))
        evidence = resolve_image(
            self.contract,
            client,
            instance_id="pyvisa_dut_validation_v1",
        )
        self.assertEqual(evidence.image_id, self.contract.lock.image_digest)
        self.assertEqual(evidence.platform, "linux/amd64")
        self.assertEqual(evidence.user, "10001:10001")
        self.assertEqual(
            evidence.dockerfile_sha256, self.contract.lock.dockerfile_sha256
        )

    def test_wrong_digest_platform_user_or_label_is_rejected(self) -> None:
        mutations = (
            (("Id",), "sha256:" + "2" * 64, "digest"),
            (("Architecture",), "arm64", "platform"),
            (("Config", "User"), "0:0", "user"),
            (
                ("Config", "Labels", "iab.dockerfile-sha256"),
                "0" * 64,
                "Dockerfile",
            ),
        )
        for keys, replacement, message in mutations:
            with self.subTest(keys=keys):
                value = valid_inspect(self.contract)
                target = value
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = replacement
                with self.assertRaisesRegex(ImagePolicyError, message):
                    resolve_image(
                        self.contract,
                        FakeDockerClient(value),
                        instance_id="pyvisa_dut_validation_v1",
                    )

    def test_build_uses_minimal_context_and_hardened_build_flags(self) -> None:
        client = FakeDockerClient(valid_inspect(self.contract))
        with tempfile.TemporaryDirectory() as directory:
            evidence = build_image(
                self.contract,
                client,
                instance_id="pyvisa_dut_validation_v1",
                temporary_root=Path(directory),
            )
            build_call = next(call for call in client.calls if call[0] == "buildx")
            self.assertIn("--network=none", build_call)
            self.assertIn("--platform=linux/amd64", build_call)
            self.assertIn("--provenance=false", build_call)
            self.assertIn("--build-arg=SOURCE_DATE_EPOCH=0", build_call)
            context = Path(build_call[-1])
            self.assertEqual(
                client.context_files,
                {"Dockerfile", "image.lock.yaml"},
            )
            self.assertTrue(
                context.resolve().is_relative_to(Path(directory).resolve())
            )
            self.assertFalse(context.exists())
            self.assertEqual(evidence.image_id, self.contract.lock.image_digest)


if __name__ == "__main__":
    unittest.main()
