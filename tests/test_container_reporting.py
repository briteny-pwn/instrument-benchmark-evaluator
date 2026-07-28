from __future__ import annotations

import dataclasses
import unittest

from evaluators.pyvisa_dut_validation_v1.scoring import (
    aggregate_reports,
    grade_run,
)
from evaluators.pyvisa_dut_validation_v1.tests.test_scoring import valid_run
from instrument_benchmark_evaluator.container.evidence import ContainerEvidence
from instrument_benchmark_evaluator.container.output import ArtifactEvidence
from instrument_benchmark_evaluator.container.runner import ContainerProcessResult
from instrument_benchmark_evaluator.run import attach_runtime_evidence


def evidence(*, cleanup: bool = True) -> ContainerEvidence:
    return ContainerEvidence(
        container_id="container-1",
        image_digest="sha256:" + "1" * 64,
        created_at="created",
        started_at="started",
        finished_at="finished",
        status="exited",
        exit_code=0,
        oom_killed=False,
        user="10001:10001",
        network_mode="none",
        readonly_rootfs=True,
        cap_drop=("ALL",),
        security_options=("no-new-privileges",),
        memory_bytes=536870912,
        nano_cpus=1000000000,
        pids_limit=64,
        pid_mode="",
        ipc_mode="private",
        uts_mode="",
        mounts=(),
        cleanup_attempted=True,
        cleanup_succeeded=cleanup,
    )


def process(status: str = "completed") -> ContainerProcessResult:
    return ContainerProcessResult(
        status=status,
        returncode=0,
        stdout="",
        stderr="",
        result={},
        container_evidence=evidence(),
        artifact_evidence=ArtifactEvidence(
            "result.json", 2, "2" * 64, 10001, 10001, 0o644
        ),
    )


class ContainerReportingTests(unittest.TestCase):
    def base_report(self):
        candidate, events, spec, snapshot = valid_run()
        return grade_run(candidate, events, spec, snapshot)

    def test_runtime_evidence_and_confidence_do_not_change_score(self) -> None:
        report = self.base_report()
        attached = attach_runtime_evidence(
            report, process(), forced_cleanup=False
        )
        self.assertEqual(attached.score, report.score)
        self.assertEqual(attached.evidence_confidence.container_runtime, 1.0)
        self.assertEqual(
            attached.container_evidence["image_digest"], "sha256:" + "1" * 64
        )
        self.assertEqual(attached.artifact_evidence["uid"], 10001)

    def test_infrastructure_invalid_is_retryable_without_score_mutation(self) -> None:
        report = self.base_report()
        invalid = attach_runtime_evidence(
            report, process("infrastructure_failure"), forced_cleanup=True
        )
        aggregate = aggregate_reports((invalid,), (report,))
        self.assertEqual(invalid.score, report.score)
        self.assertFalse(invalid.infrastructure_valid)
        self.assertTrue(invalid.retry_eligible)
        self.assertTrue(invalid.forced_cleanup)
        self.assertFalse(aggregate.infrastructure_valid)
        self.assertTrue(aggregate.retry_eligible)
        self.assertFalse(aggregate.strict_pass)

    def test_timeout_and_oom_are_candidate_outcomes_not_infrastructure(self) -> None:
        report = self.base_report()
        for status in ("candidate_timeout", "candidate_oom"):
            with self.subTest(status=status):
                attached = attach_runtime_evidence(
                    report, process(status), forced_cleanup=True
                )
                self.assertTrue(attached.infrastructure_valid)
                self.assertFalse(attached.retry_eligible)


if __name__ == "__main__":
    unittest.main()
