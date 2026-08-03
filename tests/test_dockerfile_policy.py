from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from instrument_benchmark_evaluator.container.contracts import (
    load_container_contract,
)
from instrument_benchmark_evaluator.container.dockerfile import (
    validate_dockerfile,
)
from instrument_benchmark_evaluator.container.errors import ImagePolicyError


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "dockerfiles"
INSTANCE = ROOT / "tests" / "fixtures" / "instance"


class DockerfilePolicyTests(unittest.TestCase):
    def contract_for(
        self,
        source: Path,
        *,
        context_files: dict[str, str] | None = None,
    ):
        contract = load_container_contract(INSTANCE)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return replace(
            contract,
            dockerfile=source,
            lock=replace(contract.lock, dockerfile_sha256=digest),
            context_files=context_files or contract.context_files,
        )

    def test_valid_dockerfile_returns_policy_evidence(self) -> None:
        source = FIXTURES / "valid.Dockerfile"
        evidence = validate_dockerfile(source, self.contract_for(source))
        self.assertEqual(evidence.final_user, "10001:10001")
        self.assertEqual(len(evidence.base_images), 1)
        self.assertIn("@sha256:", evidence.base_images[0])
        self.assertEqual(
            evidence.dockerfile_sha256,
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )

    def test_invalid_dockerfiles_are_rejected(self) -> None:
        cases = {
            "floating.Dockerfile": "digest-pinned",
            "remote_add.Dockerfile": "remote ADD",
            "root.Dockerfile": "USER 10001:10001",
            "candidate_copy.Dockerfile": "candidate source",
        }
        for filename, message in cases.items():
            with self.subTest(filename=filename):
                source = FIXTURES / filename
                with self.assertRaisesRegex(ImagePolicyError, message):
                    validate_dockerfile(source, self.contract_for(source))

    def test_hash_mismatch_is_rejected(self) -> None:
        source = FIXTURES / "valid.Dockerfile"
        contract = load_container_contract(INSTANCE)
        with self.assertRaisesRegex(ImagePolicyError, "hash"):
            validate_dockerfile(source, contract)

    def test_unterminated_continuation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Dockerfile"
            source.write_text(
                "FROM python@example.invalid@sha256:" + "1" * 64 + "\nRUN true \\\n"
            )
            contract = self.contract_for(source)
            with self.assertRaisesRegex(ImagePolicyError, "continuation"):
                validate_dockerfile(source, contract)

    def test_copy_accepts_only_declared_regular_context_trees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Dockerfile"
            source.write_text(
                "FROM example.invalid/python@sha256:" + "1" * 64 + "\n"
                "COPY pyvisa_iab /opt/iab/pyvisa_iab\n"
                "COPY runtime/wheelhouse /opt/iab/wheelhouse\n"
                "USER 10001:10001\n"
            )
            context = {
                "Dockerfile": "0" * 64,
                "image.lock.yaml": "0" * 64,
                "pyvisa_iab/highlevel.py": "0" * 64,
                "runtime/wheelhouse/pyvisa.whl": "0" * 64,
            }
            evidence = validate_dockerfile(
                source,
                self.contract_for(source, context_files=context),
            )
            self.assertEqual(evidence.final_user, "10001:10001")

    def test_copy_rejects_undeclared_glob_stage_and_hidden_sources(self) -> None:
        cases = (
            ("COPY undeclared.py /opt/iab/\n", "declared context"),
            ("COPY *.py /opt/iab/\n", "wildcards"),
            ("COPY --from=builder /opt/tool /opt/tool\n", "build stages"),
            ("COPY service /opt/iab/service\n", "hidden material"),
        )
        for copy_line, message in cases:
            with self.subTest(copy_line=copy_line):
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "Dockerfile"
                    source.write_text(
                        "FROM example.invalid/python@sha256:" + "1" * 64 + "\n"
                        + copy_line
                        + "USER 10001:10001\n"
                    )
                    context = {
                        "Dockerfile": "0" * 64,
                        "image.lock.yaml": "0" * 64,
                    }
                    with self.assertRaisesRegex(ImagePolicyError, message):
                        validate_dockerfile(
                            source,
                            self.contract_for(source, context_files=context),
                        )


if __name__ == "__main__":
    unittest.main()
