from __future__ import annotations

from pathlib import Path

import pytest

from evaluators.fibsem_liftout_v1.geometry.connectivity import contact_metrics
from evaluators.fibsem_liftout_v1.geometry.metrics import (
    Bounds,
    MaterialChange,
    MeshPart,
    PoseState,
    SceneSnapshot,
    TriangleMesh,
    box_mesh,
)
from evaluators.fibsem_liftout_v1.geometry.oracle import GeometryOracle
from evaluators.fibsem_liftout_v1.models import ScenarioSpec


ROOT = Path(__file__).resolve().parents[3]
NOMINAL = ROOT.parent / "instance" / "fibsem_liftout_v1" / "scenarios" / "nominal.json"


def tetra(vertices: tuple[tuple[float, float, float], ...]) -> TriangleMesh:
    return TriangleMesh(
        vertices=vertices,
        faces=((0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3)),
    )


def test_overlapping_bounds_without_surface_contact_are_not_connected() -> None:
    left = tetra(
        ((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 2.0))
    )
    right = tetra(
        ((2.0, 2.0, 2.0), (0.8, 2.0, 2.0), (2.0, 0.8, 2.0), (2.0, 2.0, 0.8))
    )

    assert left.bounds.overlaps(right.bounds)
    result = contact_metrics(left, right, epsilon_um=0.05, min_section_um=0.2)

    assert result.minimum_distance_um > 0.05
    assert not result.connected


def test_deposition_bridge_connects_sample_to_needle_with_sufficient_section() -> None:
    sample = box_mesh(center=(0.0, 0.0, 0.0), size=(14.0, 8.0, 10.0))
    needle = box_mesh(center=(-10.0, 0.0, 0.0), size=(4.0, 2.0, 2.0))
    bridge = box_mesh(center=(-7.0, 0.0, 0.0), size=(3.0, 1.0, 1.0))

    sample_contact = contact_metrics(sample, bridge, epsilon_um=0.05, min_section_um=0.3)
    needle_contact = contact_metrics(needle, bridge, epsilon_um=0.05, min_section_um=0.3)

    assert sample_contact.connected
    assert needle_contact.connected
    assert min(sample_contact.section_um, needle_contact.section_um) >= 0.3


def test_contained_solid_is_connected_even_when_surfaces_do_not_cross() -> None:
    outer = box_mesh(center=(0.0, 0.0, 0.0), size=(10.0, 10.0, 10.0))
    inner = box_mesh(center=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0))

    result = contact_metrics(outer, inner, epsilon_um=0.05, min_section_um=0.2)

    assert result.minimum_distance_um == 0.0
    assert result.section_um >= 1.0
    assert result.connected


def step_two_scene() -> SceneSnapshot:
    return SceneSnapshot(
        checkpoint_id="step_2",
        parts=(
            MeshPart("source", "source", box_mesh(center=(20, 0, 0), size=(8, 20, 8))),
            MeshPart("sample", "sample", box_mesh(center=(0, 0, 0), size=(14, 8, 10))),
            MeshPart("needle", "needle", box_mesh(center=(-10, 0, 0), size=(4, 2, 2))),
            MeshPart("target", "target", box_mesh(center=(-980, 0, 0), size=(20, 20, 10))),
            MeshPart(
                "needle_joint_1",
                "deposition",
                box_mesh(center=(-7, 0, 0), size=(3, 1, 1)),
                purpose="needle_joint",
            ),
        ),
        poses={
            "sample": PoseState((0, 0, 0), (0, 0, 0)),
            "needle": PoseState((-10, 0, 0), (0, 0, 0)),
        },
        planned_sample_volume_um3=1120.0,
        material_changes=(),
        needle_inserted=True,
        active_operation=False,
        collision=False,
    )


def test_oracle_derives_step_two_connectivity_volume_and_canonical_hash() -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    snapshot = step_two_scene()

    metrics = GeometryOracle(spec).evaluate(snapshot)

    assert not metrics.sample_to_source
    assert metrics.sample_to_needle
    assert not metrics.sample_to_target
    assert metrics.needle_joint_section_um >= spec.tolerances.joint_scale_um
    assert metrics.retained_sample_fraction == pytest.approx(1.0)
    assert len(metrics.canonical_geometry_hash) == 64

    reordered = SceneSnapshot(
        **{**snapshot.to_init_dict(), "parts": tuple(reversed(snapshot.parts))}
    )
    assert GeometryOracle(spec).evaluate(reordered).canonical_geometry_hash == metrics.canonical_geometry_hash


def test_oracle_measures_target_pose_and_rejects_lost_sample_volume() -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    target_world = spec.world_position("target_pose")
    tiny_sample = box_mesh(center=target_world, size=(7.0, 4.0, 5.0))
    snapshot = SceneSnapshot(
        checkpoint_id="step_4",
        parts=(
            MeshPart("sample", "sample", tiny_sample),
            MeshPart("target", "target", box_mesh(center=(-996, 0, 6), size=(6, 20, 12))),
            MeshPart(
                "target_joint_1",
                "deposition",
                box_mesh(center=(-992.0, 0, 6), size=(4, 2, 2)),
                purpose="target_joint",
            ),
        ),
        poses={"sample": PoseState(target_world, (0, 0, 0))},
        planned_sample_volume_um3=1120.0,
        material_changes=(),
        needle_inserted=False,
        active_operation=False,
        collision=False,
    )

    metrics = GeometryOracle(spec).evaluate(snapshot)

    assert metrics.sample_position_error_um == pytest.approx(0.0)
    assert metrics.sample_orientation_error_degrees == pytest.approx(0.0)
    assert metrics.retained_sample_fraction == pytest.approx(0.125)
    assert not metrics.sample_integrity_final


def test_oracle_uses_principal_connected_sample_instead_of_summing_fragments() -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    snapshot = SceneSnapshot(
        checkpoint_id="step_1",
        parts=(
            MeshPart("sample_main", "sample", box_mesh(center=(0, 0, 0), size=(7, 8, 10))),
            MeshPart("sample_fragment", "sample", box_mesh(center=(30, 0, 0), size=(7, 8, 10))),
            MeshPart("source", "source", box_mesh(center=(0, 0, -7), size=(8, 9, 4))),
        ),
        poses={"sample": PoseState((0, 0, 0), (0, 0, 0))},
        planned_sample_volume_um3=1120.0,
        material_changes=(),
        needle_inserted=False,
        active_operation=False,
        collision=False,
    )

    metrics = GeometryOracle(spec).evaluate(snapshot)

    assert metrics.sample_component_count == 2
    assert metrics.total_sample_fraction == pytest.approx(1.0)
    assert metrics.retained_sample_fraction == pytest.approx(0.5)
    assert not metrics.sample_integrity_step_1


def test_oracle_checks_each_material_change_against_its_phase_envelope() -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    snapshot = step_two_scene()
    valid = MaterialChange(
        operation="deposition",
        purpose="needle_joint",
        bounds=Bounds((-1, -1, 4), (1, 1, 6)),
        signed_volume_um3=2.0,
    )
    invalid = MaterialChange(
        operation="cut",
        purpose="source_separation",
        bounds=Bounds((100, 100, 100), (101, 101, 101)),
        signed_volume_um3=-1.0,
    )

    valid_metrics = GeometryOracle(spec).evaluate(
        SceneSnapshot(**{**snapshot.to_init_dict(), "material_changes": (valid,)})
    )
    invalid_metrics = GeometryOracle(spec).evaluate(
        SceneSnapshot(**{**snapshot.to_init_dict(), "material_changes": (valid, invalid)})
    )

    assert valid_metrics.changes_within_work_envelopes
    assert not invalid_metrics.changes_within_work_envelopes
    assert invalid_metrics.envelope_violations == ("source_separation",)


def test_oracle_measures_final_needle_retraction_from_sample_pose() -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    snapshot = step_two_scene()
    sample_position = snapshot.poses["sample"].position_um
    safe = spec.tolerances.safe_retraction_um

    near = GeometryOracle(spec).evaluate(
        SceneSnapshot(
            **{
                **snapshot.to_init_dict(),
                "checkpoint_id": "step_4",
                "poses": {
                    **snapshot.poses,
                    "needle": PoseState((sample_position[0] + safe - 0.1, 0, 0), (0, 0, 0)),
                },
                "needle_inserted": False,
            }
        )
    )
    far = GeometryOracle(spec).evaluate(
        SceneSnapshot(
            **{
                **snapshot.to_init_dict(),
                "checkpoint_id": "step_4",
                "poses": {
                    **snapshot.poses,
                    "needle": PoseState((sample_position[0] + safe + 0.1, 0, 0), (0, 0, 0)),
                },
                "needle_inserted": False,
            }
        )
    )

    assert near.needle_retraction_distance_um == pytest.approx(safe - 0.1)
    assert not near.needle_safely_retracted
    assert far.needle_safely_retracted


@pytest.mark.parametrize(
    ("operation", "purpose", "volume"),
    (("cut", "u_cut", 1.0), ("deposition", "needle_joint", -1.0)),
)
def test_material_change_rejects_wrong_signed_volume(
    operation: str, purpose: str, volume: float
) -> None:
    with pytest.raises(ValueError, match="signed volume"):
        MaterialChange(
            operation=operation,
            purpose=purpose,
            bounds=Bounds((0, 0, 0), (1, 1, 1)),
            signed_volume_um3=volume,
        )
