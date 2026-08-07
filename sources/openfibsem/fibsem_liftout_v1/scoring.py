from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from .artifact_scoring import CheckpointArtifactScore, score_checkpoint_artifacts
from .backend import OPENFIBSEM_COMMIT
from .geometry.roi import derive_roi_set, scenario_box_bounds
from .geometry.similarity import ShapeComparison, compare_shapes
from .geometry.stl_mesh import parse_stl_path
from .geometry.oracle import GeometryMetrics
from .journal import EventJournal, JournalError, validate_records
from .models import ScenarioSpec
from .reference_bundles import ReferenceBundle
from .step_rubric import (
    ScoreCap,
    StepBreakdown,
    StepEvidence,
    score_step,
)


STEP_POINTS = {"step_1": 20, "step_2": 25, "step_3": 25, "step_4": 20}
STEPS = tuple(STEP_POINTS)
FIXED_WORLDS = {"nominal", "small", "large", "needle_offset", "target_pose"}


@dataclass(frozen=True)
class CheckpointEvidence:
    step_id: str
    geometry: GeometryMetrics
    artifact_complete: bool
    artifact_digest: str
    artifact_root: Path | None = None
    artifact_evidence: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.step_id not in STEPS:
            raise ValueError("checkpoint evidence step is invalid")
        if self.artifact_complete and not _is_digest(self.artifact_digest):
            raise ValueError("checkpoint artifact digest is invalid")
        if self.artifact_root is not None:
            root = Path(self.artifact_root)
            if not root.is_absolute():
                raise ValueError("checkpoint artifact root must be absolute")
            object.__setattr__(self, "artifact_root", root)
        if self.artifact_evidence is not None:
            object.__setattr__(
                self,
                "artifact_evidence",
                MappingProxyType(dict(self.artifact_evidence)),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "geometry": self.geometry.to_dict(),
            "artifact_complete": self.artifact_complete,
            "artifact_digest": self.artifact_digest,
            "artifact_evidence": (
                dict(self.artifact_evidence)
                if self.artifact_evidence is not None
                else None
            ),
        }


@dataclass(frozen=True)
class TerminalEvidence:
    safe: bool
    simulator_idle: bool
    collision: bool
    cleanup_error: str | None

    @property
    def is_safe(self) -> bool:
        return (
            self.safe
            and self.simulator_idle
            and not self.collision
            and self.cleanup_error is None
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "safe": self.safe,
            "simulator_idle": self.simulator_idle,
            "collision": self.collision,
            "cleanup_error": self.cleanup_error,
        }


@dataclass(frozen=True)
class RuntimeEvidence:
    candidate_exit_code: int | None
    timed_out: bool
    forbidden_access: bool
    infrastructure_failure: bool
    candidate_uid: int
    simulator_uid: int
    isolation_verified: bool

    @property
    def candidate_completed(self) -> bool:
        return self.candidate_exit_code == 0 and not self.timed_out

    @property
    def identities_valid(self) -> bool:
        return self.candidate_uid == 10001 and self.simulator_uid == 11001

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_exit_code": self.candidate_exit_code,
            "timed_out": self.timed_out,
            "forbidden_access": self.forbidden_access,
            "infrastructure_failure": self.infrastructure_failure,
            "candidate_uid": self.candidate_uid,
            "simulator_uid": self.simulator_uid,
            "isolation_verified": self.isolation_verified,
        }


@dataclass(frozen=True)
class FibsemWorldReport:
    world_id: str
    category: str
    score: float | None
    strict_pass: bool
    retry_eligible: bool
    step_scores: Mapping[str, float]
    artifact_score: float
    step_breakdowns: Mapping[str, StepBreakdown]
    reference: Mapping[str, object] | None
    strict_gates: Mapping[str, bool]
    checkpoints: Mapping[str, CheckpointEvidence]
    partial_order: Mapping[str, int | None]
    terminal: TerminalEvidence
    runtime: RuntimeEvidence
    evidence_confidence: float
    candidate_container_evidence: Mapping[str, object] | None = None
    sim_container_evidence: Mapping[str, object] | None = None
    trusted_evidence: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "world_id": self.world_id,
            "category": self.category,
            "score": self.score,
            "strict_pass": self.strict_pass,
            "retry_eligible": self.retry_eligible,
            "step_scores": dict(self.step_scores),
            "artifact_score": self.artifact_score,
            "step_breakdowns": {
                name: breakdown.to_dict()
                for name, breakdown in sorted(self.step_breakdowns.items())
            },
            "reference": (
                dict(self.reference) if self.reference is not None else None
            ),
            "strict_gates": dict(self.strict_gates),
            "checkpoints": {
                name: checkpoint.to_dict()
                for name, checkpoint in sorted(self.checkpoints.items())
            },
            "partial_order": dict(self.partial_order),
            "terminal": self.terminal.to_dict(),
            "runtime": self.runtime.to_dict(),
            "evidence_confidence": self.evidence_confidence,
            "candidate_container_evidence": (
                dict(self.candidate_container_evidence)
                if self.candidate_container_evidence is not None
                else None
            ),
            "sim_container_evidence": (
                dict(self.sim_container_evidence)
                if self.sim_container_evidence is not None
                else None
            ),
            "trusted_evidence": (
                dict(self.trusted_evidence)
                if self.trusted_evidence is not None
                else None
            ),
        }


@dataclass(frozen=True)
class FibsemEvaluationReport:
    score: float | None
    strict_pass: bool
    retry_eligible: bool
    strict_gates: Mapping[str, bool]
    dimension_scores: Mapping[str, float]
    evidence_confidence: float
    worlds: tuple[FibsemWorldReport, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 5,
            "source_id": "openfibsem",
            "evaluator_id": "fibsem_liftout_v1",
            "openfibsem_commit": OPENFIBSEM_COMMIT,
            "score": self.score,
            "strict_pass": self.strict_pass,
            "retry_eligible": self.retry_eligible,
            "strict_gates": dict(self.strict_gates),
            "dimension_scores": dict(self.dimension_scores),
            "evidence_confidence": self.evidence_confidence,
            "suite": {"fixed_worlds": 5, "seeded_worlds": 5},
            "worlds": [world.to_dict() for world in self.worlds],
        }


def grade_world(
    spec: ScenarioSpec,
    journal: EventJournal,
    checkpoints: Mapping[str, CheckpointEvidence],
    terminal: TerminalEvidence,
    runtime: RuntimeEvidence,
    *,
    reference: ReferenceBundle | None = None,
    step_evidence: Mapping[str, StepEvidence] | None = None,
    artifact_scores: Mapping[str, CheckpointArtifactScore] | None = None,
) -> FibsemWorldReport:
    records = [event.to_dict() for event in journal.events]
    try:
        validate_records(records, journal.run_id, spec.scenario_id, require_terminal=True)
        journal_integrity = True
    except JournalError:
        journal_integrity = False
    markers, order_by_step, preflight, forbidden_journal = _journal_markers(journal)
    checkpoint_keys_valid = set(checkpoints) <= set(STEPS) and all(
        key == value.step_id for key, value in checkpoints.items()
    )
    resolved_steps: Mapping[str, StepEvidence]
    resolved_artifacts: Mapping[str, CheckpointArtifactScore]
    if reference is not None:
        resolved_steps, resolved_artifacts = _reference_evidence(
            spec, checkpoints, reference
        )
    elif step_evidence is not None:
        resolved_steps = step_evidence
        resolved_artifacts = artifact_scores or {}
    elif runtime.infrastructure_failure:
        resolved_steps = {}
        resolved_artifacts = {}
    else:
        resolved_steps = {
            step: _synthetic_step_evidence(step, checkpoint.geometry, spec)
            for step, checkpoint in checkpoints.items()
        }
        resolved_artifacts = {}

    step_scores: dict[str, float] = {}
    step_breakdowns: dict[str, StepBreakdown] = {}
    earlier_present = True
    for step in STEPS:
        checkpoint = checkpoints.get(step)
        evidence = resolved_steps.get(step)
        present = checkpoint is not None and evidence is not None
        earlier_present = earlier_present and present
        if evidence is None:
            breakdown = _zero_breakdown(step, "missing_or_untrusted_checkpoint")
        else:
            breakdown = score_step(step, evidence, spec)
            if not earlier_present:
                breakdown = _externally_capped(
                    breakdown, "missing_predecessor_checkpoint"
                )
        step_breakdowns[step] = breakdown
        step_scores[step] = breakdown.final_score

    artifact_count = sum(
        step in checkpoints
        and checkpoints[step].artifact_complete
        and _is_digest(checkpoints[step].artifact_digest)
        for step in STEPS
    )
    artifact_score = (
        round(sum(value.points for value in resolved_artifacts.values()), 6)
        if resolved_artifacts
        else artifact_count * 2.5
    )
    reference_record = (
        reference.identity.to_dict()
        if reference is not None
        else None
        if runtime.infrastructure_failure
        else _synthetic_reference_identity(spec)
    )
    forbidden_access = runtime.forbidden_access or forbidden_journal
    gates = {
        "preflight_complete": preflight,
        "journal_integrity": journal_integrity,
        "necessary_partial_order": all(order_by_step.values()),
        "all_checkpoint_states": all(
            step_scores[step] == STEP_POINTS[step] for step in STEPS
        ),
        "trusted_artifacts_complete": artifact_count == 4 and checkpoint_keys_valid,
        "reference_bundle_valid": reference_record is not None,
        "safe_terminal_state": terminal.is_safe,
        "no_forbidden_access": not forbidden_access,
        "runtime_completed": runtime.candidate_completed,
        "runtime_isolation": runtime.identities_valid and runtime.isolation_verified,
        "no_infrastructure_failure": not runtime.infrastructure_failure,
    }
    retry_eligible = runtime.infrastructure_failure
    score: float | None = (
        None
        if retry_eligible
        else float(sum(step_scores.values()) + artifact_score)
    )
    strict_pass = score == 100.0 and all(gates.values())
    confidence = artifact_count / 4 if journal_integrity else 0.0
    return FibsemWorldReport(
        world_id=spec.scenario_id,
        category="fixed" if spec.scenario_id in FIXED_WORLDS else "seeded",
        score=score,
        strict_pass=strict_pass,
        retry_eligible=retry_eligible,
        step_scores=step_scores,
        artifact_score=artifact_score,
        step_breakdowns=step_breakdowns,
        reference=reference_record,
        strict_gates=gates,
        checkpoints=dict(checkpoints),
        partial_order=markers,
        terminal=terminal,
        runtime=runtime,
        evidence_confidence=confidence,
    )


def _perfect_shape(geometry_hash: str) -> ShapeComparison:
    return ShapeComparison(
        candidate_volume_um3=1.0,
        reference_volume_um3=1.0,
        volume_similarity=1.0,
        voxel_iou=1.0,
        symmetric_surface_distance_um=0.0,
        hausdorff_distance_um=0.0,
        asd_score=1.0,
        hausdorff_score=1.0,
        shape_score=1.0,
        voxel_size_um=0.1,
        surface_sample_count=2_048,
        candidate_geometry_sha256=geometry_hash,
        reference_geometry_sha256=geometry_hash,
    )


def _synthetic_step_evidence(
    step: str, metrics: GeometryMetrics, spec: ScenarioSpec
) -> StepEvidence:
    names = {
        "step_1": ("sample", "cut", "protection", "source_bridge"),
        "step_2": ("sample", "source_separation", "needle_joint"),
        "step_3": ("sample", "target_joint", "target_interface"),
        "step_4": ("sample", "needle_separation", "target_joint"),
    }[step]
    shape = _perfect_shape(metrics.canonical_geometry_hash)
    shapes = {name: shape for name in names}
    if step in {"step_2", "step_3"}:
        needle_score = min(
            1.0,
            max(
                0.0,
                metrics.needle_joint_section_um
                / spec.tolerances.joint_scale_um,
            ),
        )
        if "needle_joint" in shapes:
            shapes["needle_joint"] = _shape_with_score(shape, needle_score)
    if step in {"step_3", "step_4"}:
        target_score = min(
            1.0,
            max(
                0.0,
                metrics.target_joint_section_um
                / spec.tolerances.joint_scale_um,
            ),
        )
        if "target_joint" in shapes:
            shapes["target_joint"] = _shape_with_score(shape, target_score)
    return StepEvidence(
        step,
        metrics,
        shapes,
        co_motion_score=1.0,
        trusted=True,
    )


def _shape_with_score(
    value: ShapeComparison, score: float
) -> ShapeComparison:
    return replace(
        value,
        volume_similarity=score,
        voxel_iou=score,
        asd_score=score,
        hausdorff_score=score,
        shape_score=score,
    )


def _synthetic_reference_identity(spec: ScenarioSpec) -> dict[str, object]:
    zero_digest = "0" * 64
    return {
        "schema_version": 1,
        "source_id": "openfibsem",
        "evaluator_id": "fibsem_liftout_v1",
        "scenario_id": spec.scenario_id,
        "scenario_sha256": hashlib.sha256(spec.canonical_bytes()).hexdigest(),
        "openfibsem_commit": OPENFIBSEM_COMMIT,
        "evaluator_commit": "0" * 40,
        "generator_tree_sha256": zero_digest,
        "reference_solution_sha256": zero_digest,
        "mesh_parser_version": "canonical-stl-v1",
        "algorithm_version": "stl-shape-v1",
        "parameter_sha256": zero_digest,
        "bundle_sha256": zero_digest,
        "file_sha256": {},
    }


def _zero_breakdown(step: str, reason: str) -> StepBreakdown:
    return StepBreakdown(
        step_id=step,
        raw_score=0.0,
        final_score=0.0,
        maximum_points=float(STEP_POINTS[step]),
        criteria=MappingProxyType({}),
        cap=ScoreCap(0.0, (reason,)),
    )


def _externally_capped(breakdown: StepBreakdown, reason: str) -> StepBreakdown:
    reasons = tuple(sorted({*breakdown.cap.reasons, reason}))
    return StepBreakdown(
        step_id=breakdown.step_id,
        raw_score=breakdown.raw_score,
        final_score=0.0,
        maximum_points=breakdown.maximum_points,
        criteria=breakdown.criteria,
        cap=ScoreCap(0.0, reasons),
    )


def _image_resolution(spec: ScenarioSpec) -> tuple[int, int]:
    imaging = spec.data.get("imaging")
    if not isinstance(imaging, Mapping):
        raise ValueError("scenario imaging contract is invalid")
    resolution = imaging.get("resolution")
    if (
        not isinstance(resolution, tuple)
        or len(resolution) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in resolution)
    ):
        raise ValueError("scenario imaging resolution is invalid")
    return resolution


def _reference_evidence(
    spec: ScenarioSpec,
    checkpoints: Mapping[str, CheckpointEvidence],
    reference: ReferenceBundle,
) -> tuple[dict[str, StepEvidence], dict[str, CheckpointArtifactScore]]:
    rois = derive_roi_set(reference, spec)
    step_rois = {
        "step_1": {
            "cut": rois.step_1_cut.bounds,
            "protection": rois.protected_region.bounds,
            "source_bridge": rois.source_bridge.bounds,
        },
        "step_2": {
            "source_separation": rois.step_2_source_separation.bounds,
            "needle_joint": rois.step_2_needle_deposition.bounds,
        },
        "step_3": {
            "target_joint": rois.step_3_target_deposition.bounds,
            "target_interface": rois.target_joint.bounds,
        },
        "step_4": {
            "needle_separation": rois.step_4_needle_separation.bounds,
            "target_joint": rois.target_joint.bounds,
        },
    }
    deposition_shapes = {
        "protection",
        "needle_joint",
        "target_joint",
    }
    steps: dict[str, StepEvidence] = {}
    artifacts: dict[str, CheckpointArtifactScore] = {}
    for step, checkpoint in checkpoints.items():
        if checkpoint.artifact_root is None:
            raise ValueError(f"trusted checkpoint artifact root is missing: {step}")
        artifact = score_checkpoint_artifacts(
            checkpoint.artifact_root,
            spec.scenario_id,
            step,
            expected_resolution=_image_resolution(spec),
        )
        artifacts[step] = artifact
        candidate_sample = parse_stl_path(
            checkpoint.artifact_root / "components" / "sample.stl"
        )
        candidate_deposition = parse_stl_path(
            checkpoint.artifact_root / "components" / "deposition.stl"
        )
        reference_step = reference.steps[step]
        envelope = scenario_box_bounds(spec, "work_envelopes", step)
        shapes: dict[str, ShapeComparison] = {
            "sample": compare_shapes(
                candidate_sample,
                reference_step.sample,
                envelope,
                tau_um=spec.tolerances.position_um,
                characteristic_length_um=spec.characteristic_length_um,
            )
        }
        for name, bounds in step_rois[step].items():
            uses_deposition = name in deposition_shapes
            shapes[name] = compare_shapes(
                candidate_deposition if uses_deposition else candidate_sample,
                reference_step.deposition if uses_deposition else reference_step.sample,
                bounds,
                tau_um=(
                    spec.tolerances.joint_scale_um
                    if uses_deposition
                    else spec.tolerances.position_um
                ),
                characteristic_length_um=spec.characteristic_length_um,
            )
        co_motion = float(
            checkpoint.geometry.sample_to_needle
            and not checkpoint.geometry.sample_to_source
            and not checkpoint.geometry.sample_to_target
        )
        steps[step] = StepEvidence(
            step_id=step,
            geometry=checkpoint.geometry,
            shapes=shapes,
            co_motion_score=co_motion if step == "step_2" else 1.0,
            trusted=checkpoint.artifact_complete,
        )
    return steps, artifacts


def aggregate_worlds(
    reports: Sequence[FibsemWorldReport],
) -> FibsemEvaluationReport:
    worlds = tuple(reports)
    infrastructure = any(world.retry_eligible for world in worlds)
    numeric_scores = [world.score for world in worlds if world.score is not None]
    score = (
        None
        if infrastructure or not numeric_scores
        else sum(numeric_scores) / len(numeric_scores)
    )
    fixed = [world for world in worlds if world.category == "fixed"]
    seeded = [world for world in worlds if world.category == "seeded"]
    exact_ids = {world.world_id for world in fixed} == FIXED_WORLDS and {
        world.world_id for world in seeded
    } == {f"seeded_{index:02d}" for index in range(1, 6)}
    gates = {
        "exact_ten_world_suite": len(worlds) == 10 and exact_ids,
        "all_fixed_worlds_pass": len(fixed) == 5 and all(
            world.strict_pass for world in fixed
        ),
        "at_least_four_seeded_worlds_pass": len(seeded) == 5
        and sum(world.strict_pass for world in seeded) >= 4,
        "no_unsafe_terminal_world": all(world.terminal.is_safe for world in worlds),
        "no_forbidden_access": all(
            world.strict_gates["no_forbidden_access"] for world in worlds
        ),
        "no_infrastructure_failure": not infrastructure,
        "suite_score_at_least_90": score is not None and score >= 90.0,
    }
    strict_pass = all(gates.values())
    dimensions = {
        step: _mean(
            [float(world.step_scores[step]) for world in worlds], default=0.0
        )
        for step in STEPS
    }
    dimensions["artifacts"] = _mean(
        [world.artifact_score for world in worlds], default=0.0
    )
    confidence = _mean(
        [world.evidence_confidence for world in worlds], default=0.0
    )
    return FibsemEvaluationReport(
        score=score,
        strict_pass=strict_pass,
        retry_eligible=infrastructure,
        strict_gates=gates,
        dimension_scores=dimensions,
        evidence_confidence=confidence,
        worlds=worlds,
    )


def _step_geometry_valid(
    step: str, metrics: GeometryMetrics, spec: ScenarioSpec
) -> bool:
    common = (
        not metrics.collision
        and metrics.simulator_idle
        and metrics.changes_within_work_envelopes
    )
    if step == "step_1":
        return (
            common
            and metrics.sample_to_source
            and not metrics.sample_to_needle
            and not metrics.sample_to_target
            and metrics.sample_component_count == 1
            and metrics.sample_integrity_step_1
        )
    if step == "step_2":
        return (
            common
            and not metrics.sample_to_source
            and metrics.sample_to_needle
            and not metrics.sample_to_target
            and metrics.needle_joint_section_um >= spec.tolerances.joint_scale_um
            and metrics.retained_sample_fraction >= 0.75
        )
    pose = (
        metrics.sample_position_error_um <= spec.tolerances.position_um
        and metrics.sample_orientation_error_degrees
        <= spec.tolerances.orientation_degrees
    )
    if step == "step_3":
        return (
            common
            and not metrics.sample_to_source
            and metrics.sample_to_needle
            and metrics.sample_to_target
            and metrics.needle_joint_section_um >= spec.tolerances.joint_scale_um
            and metrics.target_joint_section_um >= spec.tolerances.joint_scale_um
            and metrics.sample_integrity_final
            and pose
        )
    return (
        common
        and not metrics.sample_to_source
        and not metrics.sample_to_needle
        and metrics.sample_to_target
        and metrics.target_joint_section_um >= spec.tolerances.joint_scale_um
        and metrics.sample_integrity_final
        and metrics.needle_safely_retracted
        and pose
    )


def _journal_markers(
    journal: EventJournal,
) -> tuple[dict[str, int | None], dict[str, bool], bool, bool]:
    operations: list[tuple[int, str, Mapping[str, object], Mapping[str, object]]] = []
    checkpoints: dict[str, int] = {}
    rejected_forbidden = False
    pattern_by_id: dict[str, str] = {}
    pattern_completion: dict[str, int] = {}
    for event in journal.events:
        if event.kind == "checkpoint.exported":
            step = event.fields.get("step_id")
            if isinstance(step, str) and step not in checkpoints:
                checkpoints[step] = event.sequence
        if event.kind == "rpc.rejected":
            reason = event.fields.get("reason")
            operation = event.fields.get("operation")
            rejected_forbidden = rejected_forbidden or (
                isinstance(reason, str)
                and "not allowed" in reason
                or isinstance(operation, str)
                and operation.startswith("_")
            )
        if event.kind != "rpc.completed":
            continue
        operation = event.fields.get("operation")
        details = event.fields.get("details", {})
        result = event.fields.get("result_details", {})
        if not isinstance(operation, str) or not isinstance(details, Mapping) or not isinstance(result, Mapping):
            continue
        operations.append((event.sequence, operation, details, result))
        if operation in {"run_cut", "run_deposition"}:
            purpose, operation_id = details.get("pattern_purpose"), result.get("operation_id")
            if isinstance(purpose, str) and isinstance(operation_id, str):
                pattern_by_id[operation_id] = purpose
                if result.get("status") == "completed":
                    pattern_completion.setdefault(purpose, event.sequence)
        elif operation == "pattern_status" and result.get("status") == "completed":
            operation_id = result.get("operation_id")
            purpose = pattern_by_id.get(operation_id) if isinstance(operation_id, str) else None
            if purpose:
                pattern_completion.setdefault(purpose, event.sequence)

    def operation_sequences(name: str) -> list[int]:
        return [sequence for sequence, operation, _details, _result in operations if operation == name]

    image_by_beam: dict[object, int] = {}
    for sequence, operation, details, _result in operations:
        if operation == "acquire_image":
            image_by_beam.setdefault(details.get("beam"), sequence)
    required_preflight = [
        _first(operation_sequences("ping")),
        _first(operation_sequences("capabilities")),
        image_by_beam.get("SEM"),
        image_by_beam.get("FIB"),
        _nth(operation_sequences("move_stage"), 0),
        _nth(operation_sequences("move_stage"), 1),
        _first(operation_sequences("insert_manipulator")),
        _first(operation_sequences("move_manipulator")),
        _first(operation_sequences("retract_manipulator")),
        pattern_completion.get("preflight_cut"),
        pattern_completion.get("preflight_deposition"),
    ]
    preflight = all(value is not None for value in required_preflight)
    preflight_sequence = max(value for value in required_preflight if value is not None) if preflight else None
    destructive = min(
        (
            sequence
            for purpose in ("protection", "trench", "polish", "u_cut")
            if (sequence := pattern_completion.get(purpose)) is not None
        ),
        default=None,
    )
    source_cut = pattern_completion.get("source_separation")
    checkpoint_2 = checkpoints.get("step_2")
    movement = [
        sequence
        for sequence, operation, _details, _result in operations
        if operation in {"move_stage", "move_manipulator"}
    ]
    carry = _between(movement, source_cut, checkpoint_2, first=True)
    transfer = _between(movement, checkpoint_2, pattern_completion.get("target_joint"), first=True)
    target_pose = _between(
        movement, checkpoint_2, pattern_completion.get("target_joint"), first=False
    )
    markers: dict[str, int | None] = {
        "preflight": preflight_sequence,
        "destructive_roi": destructive,
        "step_1": checkpoints.get("step_1"),
        "needle_joint": pattern_completion.get("needle_joint"),
        "source_separation": source_cut,
        "carry": carry,
        "step_2": checkpoint_2,
        "transfer": transfer,
        "target_pose": target_pose,
        "target_joint": pattern_completion.get("target_joint"),
        "step_3": checkpoints.get("step_3"),
        "needle_separation": pattern_completion.get("needle_separation"),
        "needle_retraction": _between(
            operation_sequences("retract_manipulator"),
            checkpoints.get("step_3"),
            checkpoints.get("step_4"),
            first=True,
        ),
        "step_4": checkpoints.get("step_4"),
    }
    order_by_step = {
        "step_1": _ordered(markers, ("preflight", "destructive_roi", "step_1")),
        "step_2": _ordered(
            markers,
            ("step_1", "needle_joint", "source_separation", "carry", "step_2"),
        ),
        "step_3": _ordered(
            markers,
            ("step_2", "transfer", "target_pose", "target_joint", "step_3"),
        ),
        "step_4": _ordered(
            markers,
            ("step_3", "needle_separation", "needle_retraction", "step_4"),
        ),
    }
    return markers, order_by_step, preflight, rejected_forbidden


def _ordered(markers: Mapping[str, int | None], names: tuple[str, ...]) -> bool:
    values = [markers[name] for name in names]
    return all(value is not None for value in values) and all(
        left < right for left, right in zip(values, values[1:])  # type: ignore[arg-type]
    )


def _between(
    values: Sequence[int],
    lower: int | None,
    upper: int | None,
    *,
    first: bool,
) -> int | None:
    if lower is None or upper is None:
        return None
    selected = [value for value in values if lower < value < upper]
    if not selected:
        return None
    return min(selected) if first else max(selected)


def _first(values: Sequence[int]) -> int | None:
    return values[0] if values else None


def _nth(values: Sequence[int], index: int) -> int | None:
    return values[index] if len(values) > index else None


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _mean(values: Sequence[float], *, default: float) -> float:
    return sum(values) / len(values) if values else default
