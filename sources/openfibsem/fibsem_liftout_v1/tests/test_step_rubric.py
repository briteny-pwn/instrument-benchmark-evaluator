from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sources.openfibsem.fibsem_liftout_v1.geometry.oracle import GeometryMetrics
from sources.openfibsem.fibsem_liftout_v1.geometry.similarity import ShapeComparison
from sources.openfibsem.fibsem_liftout_v1.models import ScenarioSpec
from sources.openfibsem.fibsem_liftout_v1.step_rubric import (
    StepEvidence,
    score_step,
)


ROOT = Path(__file__).resolve().parents[4]
NOMINAL = (
    ROOT.parent
    / "instance"
    / "sources"
    / "openfibsem"
    / "fibsem_liftout_v1"
    / "scenarios"
    / "nominal.json"
)


def geometry(**changes: object) -> GeometryMetrics:
    value = GeometryMetrics(
        sample_to_source=False,
        sample_to_needle=False,
        sample_to_target=False,
        needle_joint_section_um=1.0,
        target_joint_section_um=1.0,
        sample_component_count=1,
        total_sample_fraction=1.0,
        retained_sample_fraction=1.0,
        sample_position_error_um=0.0,
        sample_orientation_error_degrees=0.0,
        sample_integrity_step_1=True,
        sample_integrity_final=True,
        changes_within_work_envelopes=True,
        envelope_violations=(),
        needle_retraction_distance_um=100.0,
        needle_safely_retracted=True,
        collision=False,
        simulator_idle=True,
        canonical_geometry_hash="a" * 64,
    )
    return replace(value, **changes)


def shape(score: float = 1.0) -> ShapeComparison:
    return ShapeComparison(
        candidate_volume_um3=1.0,
        reference_volume_um3=1.0,
        volume_similarity=score,
        voxel_iou=score,
        symmetric_surface_distance_um=0.0,
        hausdorff_distance_um=0.0,
        asd_score=score,
        hausdorff_score=score,
        shape_score=score,
        voxel_size_um=0.1,
        surface_sample_count=2_048,
        candidate_geometry_sha256="b" * 64,
        reference_geometry_sha256="c" * 64,
    )


def evidence(step_id: str, metrics: GeometryMetrics, **scores: float) -> StepEvidence:
    names = {
        "step_1": ("sample", "cut", "protection", "source_bridge"),
        "step_2": ("sample", "source_separation", "needle_joint"),
        "step_3": ("sample", "target_joint", "target_interface"),
        "step_4": ("sample", "needle_separation", "target_joint"),
    }[step_id]
    return StepEvidence(
        step_id=step_id,
        geometry=metrics,
        shapes={name: shape(scores.get(name, 1.0)) for name in names},
        co_motion_score=scores.get("co_motion", 1.0),
        trusted=True,
    )


@pytest.fixture(scope="module")
def spec() -> ScenarioSpec:
    return ScenarioSpec.from_path(NOMINAL)


def test_step_1_wrong_source_connection_caps_otherwise_good_shape(
    spec: ScenarioSpec,
) -> None:
    result = score_step(
        "step_1",
        evidence("step_1", geometry(sample_to_source=False)),
        spec,
    )

    assert result.raw_score > 15.0
    assert result.final_score == 8.0
    assert result.cap.reasons == ("sample_not_connected_to_source",)


def test_step_3_pose_decays_between_one_and_three_tolerances(
    spec: ScenarioSpec,
) -> None:
    near = score_step(
        "step_3",
        evidence(
            "step_3",
            geometry(
                sample_to_needle=True,
                sample_to_target=True,
                sample_position_error_um=1.5 * spec.tolerances.position_um,
            ),
        ),
        spec,
    )
    far = score_step(
        "step_3",
        evidence(
            "step_3",
            geometry(
                sample_to_needle=True,
                sample_to_target=True,
                sample_position_error_um=2.5 * spec.tolerances.position_um,
            ),
        ),
        spec,
    )

    assert 0 < far.criteria["target_pose"].points
    assert far.criteria["target_pose"].points < near.criteria["target_pose"].points
    assert near.criteria["target_pose"].points < 5


def test_step_2_one_sided_needle_joint_caps_at_twelve(spec: ScenarioSpec) -> None:
    result = score_step(
        "step_2",
        evidence(
            "step_2",
            geometry(
                sample_to_needle=True,
                needle_joint_section_um=0.5 * spec.tolerances.joint_scale_um,
            ),
        ),
        spec,
    )

    assert result.raw_score > 12
    assert result.final_score == 12
    assert result.cap.reasons == ("needle_joint_one_sided_or_thin",)


def test_step_4_needle_still_connected_caps_at_six(spec: ScenarioSpec) -> None:
    result = score_step(
        "step_4",
        evidence(
            "step_4",
            geometry(sample_to_needle=True, sample_to_target=True),
        ),
        spec,
    )

    assert result.raw_score > 6
    assert result.final_score == 6
    assert result.cap.reasons == ("needle_still_connected",)


def test_shape_subcriterion_awards_proportional_partial_credit(
    spec: ScenarioSpec,
) -> None:
    result = score_step(
        "step_1",
        evidence(
            "step_1",
            geometry(sample_to_source=True),
            sample=0.5,
            cut=0.25,
        ),
        spec,
    )

    assert result.criteria["sample_global"].points == 3.0
    assert result.criteria["cut_morphology"].points == 1.0
    assert result.final_score == result.raw_score


@pytest.mark.parametrize(
    ("step_id", "expected_total"),
    (("step_1", 20.0), ("step_2", 25.0), ("step_3", 25.0), ("step_4", 20.0)),
)
def test_perfect_step_criteria_sum_to_contract_total(
    step_id: str,
    expected_total: float,
    spec: ScenarioSpec,
) -> None:
    states = {
        "step_1": geometry(sample_to_source=True),
        "step_2": geometry(sample_to_needle=True),
        "step_3": geometry(sample_to_needle=True, sample_to_target=True),
        "step_4": geometry(sample_to_target=True),
    }

    result = score_step(step_id, evidence(step_id, states[step_id]), spec)

    assert sum(item.maximum_points for item in result.criteria.values()) == expected_total
    assert result.raw_score == expected_total
    assert result.final_score == expected_total
    assert result.cap.reasons == ()


def test_untrusted_checkpoint_receives_zero_with_explicit_cap(spec: ScenarioSpec) -> None:
    value = replace(
        evidence("step_3", geometry(sample_to_needle=True, sample_to_target=True)),
        trusted=False,
    )

    result = score_step("step_3", value, spec)

    assert result.raw_score == 25
    assert result.final_score == 0
    assert result.cap.maximum_points == 0
    assert result.cap.reasons == ("missing_or_untrusted_checkpoint",)
