from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import yaml

from instrument_benchmark_evaluator.container.contracts import (
    ContainerContractError,
    EvaluatorMaxima,
    effective_policy,
    load_container_contract,
)


DOCKERFILE = (
    "FROM python:3.11.9-slim-bookworm@"
    "sha256:2856e6af199e8128161abd320575eb9b341f3b76f017b5d0c9cd364f60d8a050\n"
    "USER 10001:10001\n"
)


class ContainerContractTests(unittest.TestCase):
    def make_instance(
        self,
        root: Path,
        *,
        protocol_version: int = 1,
        platform: str = "linux/amd64",
        user: str = "10001:10001",
        cpus: float = 1.0,
        memory_mb: int = 512,
        pids: int = 64,
    ) -> Path:
        dockerfile = root / "Dockerfile"
        dockerfile.write_text(DOCKERFILE, encoding="utf-8")
        dockerfile_hash = hashlib.sha256(dockerfile.read_bytes()).hexdigest()
        lock = {
            "schema_version": 1,
            "container_protocol_version": protocol_version,
            "platform": platform,
            "base_image": (
                "python:3.11.9-slim-bookworm@"
                "sha256:2856e6af199e8128161abd320575eb9b341f3b76f017b5d0c9cd364f60d8a050"
            ),
            "dockerfile_sha256": dockerfile_hash,
            "built_image": {
                "reference": "iab/test:v1",
                "digest": "sha256:" + "1" * 64,
            },
            "runtime_user": user,
        }
        lock_path = root / "image.lock.yaml"
        lock_path.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")
        lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        instance = {
            "container": {
                "protocol_version": protocol_version,
                "dockerfile": "Dockerfile",
                "lock_file": "image.lock.yaml",
                "context_files": {
                    "Dockerfile": dockerfile_hash,
                    "image.lock.yaml": lock_hash,
                },
                "platform": platform,
                "user": user,
                "workdir": "/workspace",
                "entrypoint": ["python", "/runner/bootstrap.py"],
                "gateway_path": "/run/iab/gateway.sock",
                "output_path": "/output/result.json",
                "limits": {
                    "cpus": cpus,
                    "memory_mb": memory_mb,
                    "pids": pids,
                    "timeout_seconds": 30,
                    "stdout_bytes": 1_048_576,
                    "stderr_bytes": 1_048_576,
                },
            }
        }
        (root / "instance.yaml").write_text(
            yaml.safe_dump(instance, sort_keys=False), encoding="utf-8"
        )
        return root

    def test_loads_complete_container_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract = load_container_contract(
                self.make_instance(Path(directory))
            )
            self.assertEqual(contract.protocol_version, 1)
            self.assertEqual(contract.platform, "linux/amd64")
            self.assertEqual(contract.user, "10001:10001")
            self.assertEqual(contract.limits.memory_mb, 512)
            self.assertEqual(contract.lock.runtime_user, "10001:10001")

    def test_effective_policy_never_exceeds_evaluator_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract = load_container_contract(
                self.make_instance(
                    Path(directory), cpus=4.0, memory_mb=4096, pids=512
                )
            )
            policy = effective_policy(
                contract,
                EvaluatorMaxima(cpus=1.0, memory_mb=512, pids=64),
            )
            self.assertEqual(policy.cpus, 1.0)
            self.assertEqual(policy.memory_mb, 512)
            self.assertEqual(policy.pids, 64)

    def test_rejects_wrong_protocol_platform_and_user(self) -> None:
        cases = (
            ({"protocol_version": 2}, "protocol"),
            ({"platform": "linux/arm64"}, "platform"),
            ({"user": "0:0"}, "user"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(ContainerContractError, message):
                        load_container_contract(
                            self.make_instance(Path(directory), **changes)
                        )

    def test_rejects_context_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_instance(Path(directory))
            (root / "Dockerfile").write_text(DOCKERFILE + "\n")
            with self.assertRaisesRegex(ContainerContractError, "hash"):
                load_container_contract(root)

    def test_rejects_path_escape_and_non_positive_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_instance(Path(directory))
            value = yaml.safe_load((root / "instance.yaml").read_text())
            value["container"]["dockerfile"] = "../Dockerfile"
            (root / "instance.yaml").write_text(yaml.safe_dump(value))
            with self.assertRaisesRegex(ContainerContractError, "path"):
                load_container_contract(root)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ContainerContractError, "cpus"):
                load_container_contract(
                    self.make_instance(Path(directory), cpus=0)
                )


if __name__ == "__main__":
    unittest.main()
