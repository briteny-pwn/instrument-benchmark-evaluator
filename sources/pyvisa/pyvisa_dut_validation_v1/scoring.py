from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .constraints.partial_order import (
    ConstraintResult,
    evaluate_constraints,
    normalize_events,
)
from .gateway.journal import EvidenceEvent
from .models import WorldSnapshot, WorldSpec
from .oracle.reconstruct import OracleError, OracleResult, reconstruct


WEIGHTS = {
    "discovery": 15,
    "driver": 15,
    "causal_state": 20,
    "experiment": 25,
    "safety": 10,
    "robustness": 15,
}


@dataclass(frozen=True)
class EvidenceConfidence:
    journal: float
    oracle: float
    semantics: float
    final_state: float
    scenarios: float
    infrastructure: float
    container_runtime: float = 0.0

    @property
    def total(self) -> float:
        return sum(
            (
                self.journal,
                self.oracle,
                self.semantics,
                self.final_state,
                self.scenarios,
                self.infrastructure,
                self.container_runtime,
            )
        ) / 7.0

    def to_dict(self) -> dict[str, float]:
        return {
            "journal": self.journal,
            "oracle": self.oracle,
            "semantics": self.semantics,
            "final_state": self.final_state,
            "scenarios": self.scenarios,
            "infrastructure": self.infrastructure,
            "container_runtime": self.container_runtime,
            "total": self.total,
        }


@dataclass(frozen=True)
class WorldReport:
    world_id: str
    status: str
    score: float
    dimensions: dict[str, float]
    gates: dict[str, bool]
    strict_pass: bool
    constraints: tuple[ConstraintResult, ...]
    evidence_confidence: EvidenceConfidence
    oracle_result: OracleResult | None
    device_evidence: dict[str, dict[str, bool]]
    experiment_completion: dict[str, bool]
    errors: tuple[str, ...]
    container_evidence: dict[str, Any] | None = None
    artifact_evidence: dict[str, Any] | None = None
    forced_cleanup: bool = False
    infrastructure_valid: bool = True
    retry_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "world_id": self.world_id,
            "status": self.status,
            "score": self.score,
            "dimensions": self.dimensions,
            "strict_gates": self.gates,
            "strict_pass": self.strict_pass,
            "constraints": [
                {
                    "name": item.name,
                    "passed": item.passed,
                    "message": item.message,
                    "evidence_sequences": list(item.evidence_sequences),
                }
                for item in self.constraints
            ],
            "evidence_confidence": self.evidence_confidence.to_dict(),
            "device_evidence": self.device_evidence,
            "experiment_completion": self.experiment_completion,
            "errors": list(self.errors),
            "container_evidence": self.container_evidence,
            "artifact_evidence": self.artifact_evidence,
            "forced_cleanup": self.forced_cleanup,
            "infrastructure_valid": self.infrastructure_valid,
            "retry_eligible": self.retry_eligible,
        }


@dataclass(frozen=True)
class EvaluationReport:
    fixed_reports: tuple[WorldReport, ...]
    repeated_reports: tuple[WorldReport, ...]
    fixed_world_pass_rate: float
    repeated_world_pass_rate: float
    score: float
    dimensions: dict[str, float]
    strict_gates: dict[str, bool]
    strict_pass: bool
    evidence_confidence: EvidenceConfidence
    infrastructure_valid: bool
    retry_eligible: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "source_id": "pyvisa",
            "status": "completed",
            "strict_pass": self.strict_pass,
            "score": self.score,
            "dimensions": self.dimensions,
            "strict_gates": self.strict_gates,
            "fixed_world_pass_rate": self.fixed_world_pass_rate,
            "repeated_world_pass_rate": self.repeated_world_pass_rate,
            "evidence_confidence": self.evidence_confidence.to_dict(),
            "infrastructure_valid": self.infrastructure_valid,
            "retry_eligible": self.retry_eligible,
            "worlds": [
                report.to_dict()
                for report in self.fixed_reports + self.repeated_reports
            ],
        }


def aggregate_reports(
    fixed_reports: Iterable[WorldReport],
    repeated_reports: Iterable[WorldReport],
) -> EvaluationReport:
    fixed = tuple(fixed_reports)
    repeated = tuple(repeated_reports)
    if not fixed or not repeated:
        raise ValueError("fixed and repeated reports must both be non-empty")
    fixed_rate = sum(report.strict_pass for report in fixed) / len(fixed)
    repeated_rate = sum(report.strict_pass for report in repeated) / len(repeated)
    all_reports = fixed + repeated
    dimensions = {
        name: sum(report.dimensions[name] for report in all_reports)
        / len(all_reports)
        for name in WEIGHTS
    }
    core_gate_names = {
        "five_devices_accessed",
        "oracle_agreement",
        "correct_decision",
        "safe_final_state",
        "active_close_all",
        "no_forbidden_access",
    }
    core_gates = {
        name: all(report.gates[name] for report in fixed)
        for name in sorted(core_gate_names)
    }
    gates = {
        **core_gates,
        "fixed_world_threshold": fixed_rate == 1.0,
        "repeated_world_threshold": repeated_rate >= 0.9,
    }
    confidence = EvidenceConfidence(
        journal=sum(r.evidence_confidence.journal for r in all_reports)
        / len(all_reports),
        oracle=sum(r.evidence_confidence.oracle for r in all_reports)
        / len(all_reports),
        semantics=sum(r.evidence_confidence.semantics for r in all_reports)
        / len(all_reports),
        final_state=sum(r.evidence_confidence.final_state for r in all_reports)
        / len(all_reports),
        scenarios=(len(fixed) + len(repeated)) / (len(fixed) + len(repeated)),
        infrastructure=sum(r.evidence_confidence.infrastructure for r in all_reports)
        / len(all_reports),
        container_runtime=sum(
            r.evidence_confidence.container_runtime for r in all_reports
        )
        / len(all_reports),
    )
    infrastructure_valid = all(report.infrastructure_valid for report in all_reports)
    return EvaluationReport(
        fixed_reports=fixed,
        repeated_reports=repeated,
        fixed_world_pass_rate=fixed_rate,
        repeated_world_pass_rate=repeated_rate,
        score=sum(dimensions.values()),
        dimensions=dimensions,
        strict_gates=gates,
        strict_pass=all(gates.values()) and infrastructure_valid,
        evidence_confidence=confidence,
        infrastructure_valid=infrastructure_valid,
        retry_eligible=any(report.retry_eligible for report in all_reports),
    )


def grade_run(
    candidate_result: Mapping[str, Any] | None,
    evidence: Iterable[EvidenceEvent],
    spec: WorldSpec,
    final_snapshot: WorldSnapshot,
    *,
    forbidden_access: bool = False,
    infrastructure_ok: bool = True,
) -> WorldReport:
    records = tuple(evidence)
    semantic = normalize_events(records)
    constraints = evaluate_constraints(semantic, final_snapshot.safe)
    errors: list[str] = []
    oracle: OracleResult | None
    try:
        oracle = reconstruct(records, spec)
    except OracleError as exc:
        oracle = None
        errors.append(str(exc))

    oracle_agreement = (
        oracle is not None
        and candidate_result is not None
        and _candidate_agrees(candidate_result, oracle)
    )
    correct_decision = (
        oracle is not None
        and candidate_result is not None
        and candidate_result.get("decision") is oracle.decision
    )
    identified_roles = {
        item.role for item in semantic if item.kind == "device.identified"
    }
    five_devices = {"psu", "switch", "awg", "scope", "dmm"}.issubset(
        identified_roles
    )
    active_close = _constraint(constraints, "active_close_all")
    safe_final = _constraint(constraints, "safe_final_state")
    no_forbidden = not forbidden_access
    world_prerequisites = (
        five_devices
        and oracle_agreement
        and correct_decision
        and safe_final
        and active_close
        and no_forbidden
        and infrastructure_ok
    )
    gates = {
        "five_devices_accessed": five_devices,
        "oracle_agreement": oracle_agreement,
        "correct_decision": correct_decision,
        "safe_final_state": safe_final,
        "active_close_all": active_close,
        "no_forbidden_access": no_forbidden,
        "robustness_threshold": world_prerequisites,
    }

    process = [
        item
        for item in constraints
        if item.name not in {"active_close_all", "safe_final_state"}
    ]
    configured_roles = {
        record.role
        for record in records
        if record.operation
        in {"set_timeout", "set_read_termination", "set_write_termination"}
    }
    driver_complete = {
        "psu",
        "switch",
        "awg",
        "scope",
        "dmm",
    }.issubset(configured_roles)
    dimensions = {
        "discovery": float(WEIGHTS["discovery"] if five_devices else 0),
        "driver": float(WEIGHTS["driver"] if driver_complete else 0),
        "causal_state": _fraction(process) * WEIGHTS["causal_state"],
        "experiment": float(
            WEIGHTS["experiment"] if oracle_agreement and correct_decision else 0
        ),
        "safety": (
            (float(active_close) + float(safe_final)) / 2.0 * WEIGHTS["safety"]
        ),
        "robustness": float(
            WEIGHTS["robustness"] if world_prerequisites else 0
        ),
    }
    confidence = _confidence(
        records,
        oracle is not None,
        semantic,
        final_snapshot is not None,
        infrastructure_ok,
    )
    device_evidence = _device_evidence(semantic)
    kinds = {item.kind for item in semantic}
    experiment_completion = {
        "routes_configured": "switch.routes_closed" in kinds,
        "power_enabled": "psu.output_on" in kinds,
        "stimulus_configured": {
            "awg.waveform_uploaded",
            "awg.waveform_selected",
            "awg.configured",
            "awg.output_on",
        }.issubset(kinds),
        "settled": "dut.settled" in kinds,
        "dmm_acquired": "dmm.acquired" in kinds,
        "scope_acquired": "scope.acquired" in kinds,
        "oracle_derived": oracle is not None,
        "safe_cleanup": active_close and safe_final,
    }
    return WorldReport(
        world_id=spec.world_id,
        status="completed" if infrastructure_ok else "infrastructure_failure",
        score=sum(dimensions.values()),
        dimensions=dimensions,
        gates=gates,
        strict_pass=all(gates.values()) and infrastructure_ok,
        constraints=constraints,
        evidence_confidence=confidence,
        oracle_result=oracle,
        device_evidence=device_evidence,
        experiment_completion=experiment_completion,
        errors=tuple(errors),
    )


def _candidate_agrees(
    candidate: Mapping[str, Any], oracle: OracleResult
) -> bool:
    expected = oracle.to_candidate_result()
    for section in ("measurements", "derived"):
        actual_section = candidate.get(section)
        if not isinstance(actual_section, Mapping):
            return False
        for key, expected_value in expected[section].items():
            if key not in actual_section or not _equivalent(
                actual_section[key], expected_value
            ):
                return False
    return candidate.get("decision") is oracle.decision


def _equivalent(actual: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_equivalent(left, right) for left, right in zip(actual, expected))
        )
    if isinstance(expected, (float, int)) and not isinstance(expected, bool):
        return (
            isinstance(actual, (float, int))
            and not isinstance(actual, bool)
            and math.isclose(float(actual), float(expected), rel_tol=1e-7, abs_tol=1e-7)
        )
    return actual == expected


def _constraint(results: Iterable[ConstraintResult], name: str) -> bool:
    return next(result.passed for result in results if result.name == name)


def _fraction(results: Iterable[ConstraintResult]) -> float:
    values = tuple(results)
    return (
        sum(1 for result in values if result.passed) / len(values)
        if values
        else 0.0
    )


def _confidence(
    records: tuple[EvidenceEvent, ...],
    oracle_ok: bool,
    semantic,
    final_state_observed: bool,
    infrastructure_ok: bool,
) -> EvidenceConfidence:
    complete_records = [
        record
        for record in records
        if record.request_sha256
        and record.response_sha256
        and record.state_before_sha256
        and record.state_after_sha256
    ]
    journal_coverage = len(complete_records) / len(records) if records else 0.0
    kinds = {item.kind for item in semantic}
    expected = {
        "device.identified",
        "switch.routes_closed",
        "psu.configured",
        "psu.output_on",
        "awg.waveform_uploaded",
        "awg.waveform_selected",
        "awg.output_on",
        "dut.settled",
        "dmm.acquired",
        "scope.acquired",
        "awg.output_off",
        "psu.output_off",
        "switch.routes_opened",
        "resource.closed",
    }
    return EvidenceConfidence(
        journal=journal_coverage,
        oracle=float(oracle_ok),
        semantics=len(kinds & expected) / len(expected),
        final_state=float(final_state_observed),
        scenarios=1.0,
        infrastructure=float(infrastructure_ok),
    )


def _device_evidence(semantic) -> dict[str, dict[str, bool]]:
    roles = ("psu", "switch", "awg", "scope", "dmm")
    result: dict[str, dict[str, bool]] = {}
    for role in roles:
        role_events = [item for item in semantic if item.role == role]
        kinds = {item.kind for item in role_events}
        opened_sessions = {
            item.session_digest
            for item in role_events
            if item.kind == "resource.opened"
        }
        closed_sessions = {
            item.session_digest
            for item in role_events
            if item.kind == "resource.closed"
            and item.cleanup_source == "candidate"
        }
        configured_kinds = {
            "psu": {"psu.configured"},
            "switch": {"switch.routes_closed"},
            "awg": {
                "awg.waveform_uploaded",
                "awg.waveform_selected",
                "awg.configured",
            },
            "scope": {"scope.configured"},
            "dmm": {"dmm.configured"},
        }[role]
        acquired_kind = {
            "scope": "scope.acquired",
            "dmm": "dmm.acquired",
        }.get(role)
        result[role] = {
            "opened": bool(opened_sessions),
            "identified": "device.identified" in kinds,
            "configured": configured_kinds.issubset(kinds),
            "acquired": acquired_kind in kinds if acquired_kind else True,
            "active_close": bool(opened_sessions)
            and opened_sessions == closed_sessions,
        }
    return result
