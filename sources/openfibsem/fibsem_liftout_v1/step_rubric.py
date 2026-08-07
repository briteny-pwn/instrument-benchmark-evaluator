from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .geometry.oracle import GeometryMetrics
from .geometry.similarity import ShapeComparison
from .models import ScenarioSpec


STEP_TOTALS = {"step_1": 20.0, "step_2": 25.0, "step_3": 25.0, "step_4": 20.0}


@dataclass(frozen=True)
class CriterionScore:
    criterion_id: str
    points: float
    maximum_points: float
    metrics: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "criterion_id": self.criterion_id,
            "points": self.points,
            "maximum_points": self.maximum_points,
            "metrics": dict(sorted(self.metrics.items())),
        }


@dataclass(frozen=True)
class ScoreCap:
    maximum_points: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "maximum_points": self.maximum_points,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class StepBreakdown:
    step_id: str
    raw_score: float
    final_score: float
    maximum_points: float
    criteria: Mapping[str, CriterionScore]
    cap: ScoreCap

    def to_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "raw_score": self.raw_score,
            "final_score": self.final_score,
            "maximum_points": self.maximum_points,
            "criteria": {
                name: criterion.to_dict()
                for name, criterion in sorted(self.criteria.items())
            },
            "cap": self.cap.to_dict(),
        }


@dataclass(frozen=True)
class StepEvidence:
    step_id: str
    geometry: GeometryMetrics
    shapes: Mapping[str, ShapeComparison]
    co_motion_score: float = 0.0
    trusted: bool = True

    def __post_init__(self) -> None:
        if self.step_id not in STEP_TOTALS:
            raise ValueError("step evidence ID is invalid")
        if (
            isinstance(self.co_motion_score, bool)
            or not math.isfinite(self.co_motion_score)
            or not 0.0 <= self.co_motion_score <= 1.0
        ):
            raise ValueError("co-motion score must be between zero and one")
        if any(
            not isinstance(name, str) or not isinstance(value, ShapeComparison)
            for name, value in self.shapes.items()
        ):
            raise ValueError("step shape evidence is invalid")
        object.__setattr__(self, "shapes", MappingProxyType(dict(self.shapes)))


def _rounded(value: float) -> float:
    return round(value, 6)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _criterion(
    criterion_id: str,
    fraction: float,
    maximum: float,
    **metrics: object,
) -> CriterionScore:
    return CriterionScore(
        criterion_id=criterion_id,
        points=_rounded(_clamp01(fraction) * maximum),
        maximum_points=maximum,
        metrics=MappingProxyType(dict(sorted(metrics.items()))),
    )


def _shape_fraction(evidence: StepEvidence, name: str) -> float:
    value = evidence.shapes.get(name)
    return 0.0 if value is None else _clamp01(value.shape_score)


def _shape_criterion(
    criterion_id: str,
    evidence: StepEvidence,
    shape_name: str,
    maximum: float,
) -> CriterionScore:
    value = evidence.shapes.get(shape_name)
    if value is None:
        return _criterion(
            criterion_id,
            0.0,
            maximum,
            missing_shape_evidence=True,
        )
    return _criterion(
        criterion_id,
        value.shape_score,
        maximum,
        candidate_volume_um3=value.candidate_volume_um3,
        reference_volume_um3=value.reference_volume_um3,
        volume_similarity=value.volume_similarity,
        voxel_iou=value.voxel_iou,
        symmetric_surface_distance_um=value.symmetric_surface_distance_um,
        hausdorff_distance_um=value.hausdorff_distance_um,
        asd_score=value.asd_score,
        hausdorff_score=value.hausdorff_score,
        shape_score=value.shape_score,
        voxel_size_um=value.voxel_size_um,
        surface_sample_count=value.surface_sample_count,
        candidate_geometry_sha256=value.candidate_geometry_sha256,
        reference_geometry_sha256=value.reference_geometry_sha256,
    )


def _threshold_fraction(value: float, minimum: float, full: float) -> float:
    if value <= minimum:
        return 0.0
    if value >= full:
        return 1.0
    return (value - minimum) / (full - minimum)


def _pose_fraction(error: float, tolerance: float) -> float:
    if error <= tolerance:
        return 1.0
    if error >= 3.0 * tolerance:
        return 0.0
    return (3.0 - error / tolerance) / 2.0


def _common_fraction(metrics: GeometryMetrics) -> float:
    points = (
        (0.5 if not metrics.collision else 0.0)
        + (0.5 if metrics.simulator_idle else 0.0)
        + (1.0 if metrics.changes_within_work_envelopes else 0.0)
    )
    return points / 2.0


def _step_1(evidence: StepEvidence, spec: ScenarioSpec) -> dict[str, CriterionScore]:
    metrics = evidence.geometry
    bridge_fraction = (
        _shape_fraction(evidence, "source_bridge")
        + (1.0 if metrics.sample_to_source else 0.0)
    ) / 2.0
    integrity_fraction = (
        (1.0 if metrics.sample_component_count == 1 else 0.0)
        + _threshold_fraction(metrics.retained_sample_fraction, 0.65, 0.75)
    ) / 2.0
    return {
        "sample_global": _shape_criterion("sample_global", evidence, "sample", 6.0),
        "cut_morphology": _shape_criterion("cut_morphology", evidence, "cut", 4.0),
        "protection_morphology": _shape_criterion(
            "protection_morphology", evidence, "protection", 3.0
        ),
        "source_bridge": _criterion(
            "source_bridge",
            bridge_fraction,
            3.0,
            sample_to_source=metrics.sample_to_source,
        ),
        "sample_integrity": _criterion(
            "sample_integrity",
            integrity_fraction,
            2.0,
            component_count=metrics.sample_component_count,
            retained_fraction=_rounded(metrics.retained_sample_fraction),
        ),
        "common_state": _criterion(
            "common_state", _common_fraction(metrics), 2.0
        ),
    }


def _step_2(evidence: StepEvidence, spec: ScenarioSpec) -> dict[str, CriterionScore]:
    metrics = evidence.geometry
    state_fraction = sum(
        (
            not metrics.sample_to_source,
            metrics.sample_to_needle,
            not metrics.sample_to_target,
        )
    ) / 3.0
    joint_fraction = _clamp01(
        metrics.needle_joint_section_um / spec.tolerances.joint_scale_um
    )
    connectivity_points = 3.0 * state_fraction + 2.0 * joint_fraction
    return {
        "sample_preservation": _shape_criterion(
            "sample_preservation", evidence, "sample", 5.0
        ),
        "source_separation": _shape_criterion(
            "source_separation", evidence, "source_separation", 5.0
        ),
        "needle_joint": _shape_criterion(
            "needle_joint", evidence, "needle_joint", 5.0
        ),
        "transfer_connectivity": _criterion(
            "transfer_connectivity",
            connectivity_points / 5.0,
            5.0,
            sample_to_source=metrics.sample_to_source,
            sample_to_needle=metrics.sample_to_needle,
            sample_to_target=metrics.sample_to_target,
            needle_joint_section_um=_rounded(metrics.needle_joint_section_um),
        ),
        "co_motion": _criterion(
            "co_motion", evidence.co_motion_score, 3.0
        ),
        "common_state": _criterion(
            "common_state", _common_fraction(metrics), 2.0
        ),
    }


def _step_3(evidence: StepEvidence, spec: ScenarioSpec) -> dict[str, CriterionScore]:
    metrics = evidence.geometry
    position_fraction = _pose_fraction(
        metrics.sample_position_error_um, spec.tolerances.position_um
    )
    orientation_fraction = _pose_fraction(
        metrics.sample_orientation_error_degrees,
        spec.tolerances.orientation_degrees,
    )
    pose_points = 3.0 * position_fraction + 2.0 * orientation_fraction
    state_fraction = sum(
        (
            not metrics.sample_to_source,
            metrics.sample_to_needle,
            metrics.sample_to_target,
        )
    ) / 3.0
    needle_fraction = _clamp01(
        metrics.needle_joint_section_um / spec.tolerances.joint_scale_um
    )
    target_fraction = _clamp01(
        metrics.target_joint_section_um / spec.tolerances.joint_scale_um
    )
    connectivity_points = 3.0 * state_fraction + needle_fraction + target_fraction
    return {
        "sample_preservation": _shape_criterion(
            "sample_preservation", evidence, "sample", 4.0
        ),
        "target_pose": _criterion(
            "target_pose",
            pose_points / 5.0,
            5.0,
            position_error_um=_rounded(metrics.sample_position_error_um),
            orientation_error_degrees=_rounded(
                metrics.sample_orientation_error_degrees
            ),
        ),
        "target_joint": _shape_criterion(
            "target_joint", evidence, "target_joint", 5.0
        ),
        "dual_connectivity": _criterion(
            "dual_connectivity",
            connectivity_points / 5.0,
            5.0,
            sample_to_needle=metrics.sample_to_needle,
            sample_to_target=metrics.sample_to_target,
            needle_joint_section_um=_rounded(metrics.needle_joint_section_um),
            target_joint_section_um=_rounded(metrics.target_joint_section_um),
        ),
        "target_interface": _shape_criterion(
            "target_interface", evidence, "target_interface", 4.0
        ),
        "common_state": _criterion(
            "common_state", _common_fraction(metrics), 2.0
        ),
    }


def _step_4(evidence: StepEvidence, spec: ScenarioSpec) -> dict[str, CriterionScore]:
    metrics = evidence.geometry
    final_topology = sum(
        (
            not metrics.sample_to_source,
            not metrics.sample_to_needle,
            metrics.sample_to_target,
        )
    ) / 3.0
    position_fraction = _pose_fraction(
        metrics.sample_position_error_um, spec.tolerances.position_um
    )
    orientation_fraction = _pose_fraction(
        metrics.sample_orientation_error_degrees,
        spec.tolerances.orientation_degrees,
    )
    pose_points = 1.2 * position_fraction + 0.8 * orientation_fraction
    retraction_points = (
        (1.5 if metrics.needle_safely_retracted else 0.0)
        + (0.5 if metrics.simulator_idle else 0.0)
    )
    return {
        "sample_preservation": _shape_criterion(
            "sample_preservation", evidence, "sample", 4.0
        ),
        "needle_separation": _shape_criterion(
            "needle_separation", evidence, "needle_separation", 4.0
        ),
        "target_joint_preservation": _shape_criterion(
            "target_joint_preservation", evidence, "target_joint", 4.0
        ),
        "final_topology": _criterion(
            "final_topology",
            final_topology,
            4.0,
            sample_to_source=metrics.sample_to_source,
            sample_to_needle=metrics.sample_to_needle,
            sample_to_target=metrics.sample_to_target,
        ),
        "target_pose": _criterion(
            "target_pose",
            pose_points / 2.0,
            2.0,
            position_error_um=_rounded(metrics.sample_position_error_um),
            orientation_error_degrees=_rounded(
                metrics.sample_orientation_error_degrees
            ),
        ),
        "safe_retraction": _criterion(
            "safe_retraction",
            retraction_points / 2.0,
            2.0,
            needle_safely_retracted=metrics.needle_safely_retracted,
            simulator_idle=metrics.simulator_idle,
        ),
    }


def _caps(
    step_id: str,
    evidence: StepEvidence,
    spec: ScenarioSpec,
) -> ScoreCap:
    total = STEP_TOTALS[step_id]
    if not evidence.trusted:
        return ScoreCap(0.0, ("missing_or_untrusted_checkpoint",))
    metrics = evidence.geometry
    applicable: list[tuple[float, str]] = []
    fragmented = (
        metrics.sample_component_count != 1
        or metrics.retained_sample_fraction < 0.65
    )
    if step_id == "step_1":
        if not metrics.sample_to_source:
            applicable.append((8.0, "sample_not_connected_to_source"))
        if metrics.sample_to_needle:
            applicable.append((8.0, "early_needle_connection"))
        if metrics.sample_to_target:
            applicable.append((8.0, "early_target_connection"))
        if fragmented:
            applicable.append((5.0, "sample_fragmented_or_below_65_percent"))
        if metrics.collision or not metrics.changes_within_work_envelopes:
            applicable.append((5.0, "collision_or_outside_work_envelope"))
    elif step_id == "step_2":
        if metrics.sample_to_source:
            applicable.append((10.0, "source_still_connected"))
        if not metrics.sample_to_needle:
            applicable.append((10.0, "needle_not_connected"))
        if metrics.sample_to_target:
            applicable.append((8.0, "target_connected_too_early"))
        if metrics.sample_to_needle and (
            metrics.needle_joint_section_um < spec.tolerances.joint_scale_um
        ):
            applicable.append((12.0, "needle_joint_one_sided_or_thin"))
        if fragmented:
            applicable.append((6.0, "sample_fragmented_or_below_65_percent"))
    elif step_id == "step_3":
        if not metrics.sample_to_target:
            applicable.append((10.0, "target_not_connected"))
        if not metrics.sample_to_needle:
            applicable.append((10.0, "needle_disconnected_too_early"))
        if metrics.sample_to_source:
            applicable.append((5.0, "source_reconnected"))
        if metrics.sample_position_error_um > 3.0 * spec.tolerances.position_um:
            applicable.append((12.0, "target_position_beyond_three_tolerances"))
        if metrics.sample_to_target and (
            metrics.target_joint_section_um < spec.tolerances.joint_scale_um
        ):
            applicable.append((12.0, "target_joint_one_sided_or_thin"))
        if fragmented:
            applicable.append((8.0, "sample_fragmented_or_below_65_percent"))
    else:
        if not metrics.sample_to_target:
            applicable.append((6.0, "target_not_connected"))
        if metrics.sample_to_needle:
            applicable.append((6.0, "needle_still_connected"))
        if metrics.sample_to_source:
            applicable.append((4.0, "source_still_connected"))
        if not metrics.needle_safely_retracted:
            applicable.append((10.0, "unsafe_needle_retraction"))
        if fragmented:
            applicable.append((6.0, "sample_fragmented_or_below_65_percent"))
        if metrics.sample_position_error_um > 3.0 * spec.tolerances.position_um:
            applicable.append((10.0, "target_position_beyond_three_tolerances"))
    if not applicable:
        return ScoreCap(total, ())
    return ScoreCap(
        min(maximum for maximum, _reason in applicable),
        tuple(sorted(reason for _maximum, reason in applicable)),
    )


def score_step(
    step_id: str,
    evidence: StepEvidence,
    spec: ScenarioSpec,
) -> StepBreakdown:
    if step_id not in STEP_TOTALS or evidence.step_id != step_id:
        raise ValueError("step rubric identity mismatch")
    scorers = {
        "step_1": _step_1,
        "step_2": _step_2,
        "step_3": _step_3,
        "step_4": _step_4,
    }
    criteria = scorers[step_id](evidence, spec)
    raw_score = _rounded(sum(value.points for value in criteria.values()))
    cap = _caps(step_id, evidence, spec)
    return StepBreakdown(
        step_id=step_id,
        raw_score=raw_score,
        final_score=_rounded(min(raw_score, cap.maximum_points)),
        maximum_points=STEP_TOTALS[step_id],
        criteria=MappingProxyType(dict(sorted(criteria.items()))),
        cap=cap,
    )


__all__ = [
    "CriterionScore",
    "ScoreCap",
    "StepBreakdown",
    "StepEvidence",
    "score_step",
]
