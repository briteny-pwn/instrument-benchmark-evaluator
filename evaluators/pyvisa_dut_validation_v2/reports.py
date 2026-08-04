from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evaluators.pyvisa_dut_validation_v1.scoring import (
    EvaluationReport,
    WorldReport,
)


@dataclass(frozen=True)
class V2WorldReport:
    base: WorldReport
    candidate_container_evidence: dict[str, Any] | None
    sim_container_evidence: dict[str, Any] | None
    sim_journal_evidence: dict[str, Any] | None

    def __post_init__(self) -> None:
        if self.base.container_evidence != self.candidate_container_evidence:
            raise ValueError(
                "candidate evidence must rename base container evidence"
            )
        missing = any(
            value is None
            for value in (
                self.candidate_container_evidence,
                self.sim_container_evidence,
                self.sim_journal_evidence,
            )
        )
        if missing and not (
            not self.base.infrastructure_valid and self.base.retry_eligible
        ):
            raise ValueError(
                "missing v2 evidence requires retryable infrastructure failure"
            )

    def to_dict(self) -> dict[str, Any]:
        value = self.base.to_dict()
        value.pop("container_evidence", None)
        value["candidate_container_evidence"] = self.candidate_container_evidence
        value["sim_container_evidence"] = self.sim_container_evidence
        value["sim_journal_evidence"] = self.sim_journal_evidence
        return value


@dataclass(frozen=True)
class V2EvaluationReport:
    base: EvaluationReport
    worlds: tuple[V2WorldReport, ...]

    def __post_init__(self) -> None:
        if (
            len(self.base.fixed_reports) != 9
            or len(self.base.repeated_reports) != 10
        ):
            raise ValueError(
                "v2 evaluation requires nine fixed and ten repeated worlds"
            )
        expected = self.base.fixed_reports + self.base.repeated_reports
        if len(self.worlds) != len(expected) or any(
            wrapped.base != base
            for wrapped, base in zip(self.worlds, expected, strict=True)
        ):
            raise ValueError("v2 worlds do not match base evaluation report")

    def to_dict(self) -> dict[str, Any]:
        value = self.base.to_dict()
        value["schema_version"] = 2
        value["worlds"] = [world.to_dict() for world in self.worlds]
        return value
