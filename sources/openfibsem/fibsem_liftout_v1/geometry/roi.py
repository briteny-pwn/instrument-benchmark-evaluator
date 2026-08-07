from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from ..models import ScenarioSpec, vec3
from .metrics import Bounds, TriangleMesh, Vec
from .stl_mesh import CanonicalMesh


@dataclass(frozen=True)
class Roi:
    roi_id: str
    frame: str
    bounds: Bounds


@dataclass(frozen=True)
class RoiSet:
    protected_region: Roi
    source_bridge: Roi
    needle_joint: Roi
    target_joint: Roi
    step_1_cut: Roi
    step_2_source_separation: Roi
    step_2_needle_deposition: Roi
    step_3_target_deposition: Roi
    step_4_needle_separation: Roi


class _ReferenceStep(Protocol):
    sample: CanonicalMesh
    deposition: CanonicalMesh


class _ReferenceBundle(Protocol):
    baseline_sample: CanonicalMesh
    steps: Mapping[str, _ReferenceStep]


def _box_bounds(spec: ScenarioSpec, value: object, name: str) -> Bounds:
    if not isinstance(value, Mapping) or set(value) != {
        "frame",
        "center_um",
        "size_um",
    }:
        raise ValueError(f"scenario ROI {name} is invalid")
    frame = value["frame"]
    if not isinstance(frame, str) or frame not in spec.frames:
        raise ValueError(f"scenario ROI {name} frame is invalid")
    center = vec3(value["center_um"], f"{name}.center_um")
    size = vec3(value["size_um"], f"{name}.size_um")
    if min(size) <= 0:
        raise ValueError(f"scenario ROI {name} size must be positive")
    origin = spec.world_position(frame)
    world_center = tuple(
        origin[index] + center[index] for index in range(3)
    )
    return Bounds(
        tuple(world_center[index] - size[index] / 2 for index in range(3)),
        tuple(world_center[index] + size[index] / 2 for index in range(3)),
    )  # type: ignore[arg-type]


def _scenario_box(spec: ScenarioSpec, section: str, name: str) -> Bounds:
    parent = spec.data.get(section)
    if not isinstance(parent, Mapping) or name not in parent:
        raise ValueError(f"scenario ROI {section}.{name} is missing")
    return _box_bounds(spec, parent[name], f"{section}.{name}")


def scenario_box_bounds(spec: ScenarioSpec, section: str, name: str) -> Bounds:
    """Resolve one named scenario box into the world coordinate frame."""

    return _scenario_box(spec, section, name)


def _expanded(bounds: Bounds, padding: float) -> Bounds:
    return Bounds(
        tuple(value - padding for value in bounds.minimum),
        tuple(value + padding for value in bounds.maximum),
    )  # type: ignore[arg-type]


def _intersection(left: Bounds, right: Bounds) -> Bounds | None:
    minimum = tuple(max(left.minimum[index], right.minimum[index]) for index in range(3))
    maximum = tuple(min(left.maximum[index], right.maximum[index]) for index in range(3))
    if any(maximum[index] < minimum[index] for index in range(3)):
        return None
    return Bounds(minimum, maximum)  # type: ignore[arg-type]


def _triangle_signatures(mesh: TriangleMesh) -> set[tuple[Vec, Vec, Vec]]:
    return {tuple(sorted(triangle)) for triangle in mesh.triangles}  # type: ignore[return-value]


def _difference_points(before: CanonicalMesh, after: CanonicalMesh) -> tuple[Vec, ...]:
    before_triangles = _triangle_signatures(before.mesh)
    after_triangles = _triangle_signatures(after.mesh)
    changed = before_triangles.symmetric_difference(after_triangles)
    return tuple(vertex for triangle in sorted(changed) for vertex in triangle)


def _point_inside(point: Vec, bounds: Bounds) -> bool:
    return all(
        bounds.minimum[index] <= point[index] <= bounds.maximum[index]
        for index in range(3)
    )


def _delta_roi(
    roi_id: str,
    before: CanonicalMesh,
    after: CanonicalMesh,
    *,
    envelope: Bounds,
    fallback: Bounds,
    padding: float,
) -> Roi:
    points = tuple(
        point for point in _difference_points(before, after) if _point_inside(point, envelope)
    )
    if points:
        bounds = Bounds(
            tuple(min(point[index] for point in points) for index in range(3)),
            tuple(max(point[index] for point in points) for index in range(3)),
        )
        clipped = _intersection(_expanded(bounds, padding), envelope)
    else:
        clipped = _intersection(_expanded(fallback, padding), envelope)
    if clipped is None:
        raise ValueError(f"reference delta ROI {roi_id} is outside its step envelope")
    return Roi(roi_id=roi_id, frame="world", bounds=clipped)


def derive_roi_set(reference: _ReferenceBundle, spec: ScenarioSpec) -> RoiSet:
    """Derive hard and reference-difference ROIs in the world frame."""

    protected = _scenario_box(spec, "sample", "protected_region")
    source_bridge = _scenario_box(spec, "sample", "source_bridge")
    needle_joint = _scenario_box(spec, "needle", "joint_region")
    target_joint = _scenario_box(spec, "target", "joint_region")
    envelopes = {
        step: _scenario_box(spec, "work_envelopes", step)
        for step in ("step_1", "step_2", "step_3", "step_4")
    }
    steps = reference.steps
    if set(steps) != {"step_1", "step_2", "step_3", "step_4"}:
        raise ValueError("reference steps are incomplete")
    position_padding = spec.tolerances.position_um
    joint_padding = spec.tolerances.joint_scale_um
    return RoiSet(
        protected_region=Roi("protected_region", "world", protected),
        source_bridge=Roi("source_bridge", "world", source_bridge),
        needle_joint=Roi("needle_joint", "world", needle_joint),
        target_joint=Roi("target_joint", "world", target_joint),
        step_1_cut=_delta_roi(
            "step_1_cut",
            reference.baseline_sample,
            steps["step_1"].sample,
            envelope=envelopes["step_1"],
            fallback=source_bridge,
            padding=position_padding,
        ),
        step_2_source_separation=_delta_roi(
            "step_2_source_separation",
            steps["step_1"].sample,
            steps["step_2"].sample,
            envelope=envelopes["step_2"],
            fallback=source_bridge,
            padding=max(joint_padding, position_padding),
        ),
        step_2_needle_deposition=_delta_roi(
            "step_2_needle_deposition",
            steps["step_1"].deposition,
            steps["step_2"].deposition,
            envelope=envelopes["step_2"],
            fallback=needle_joint,
            padding=joint_padding,
        ),
        step_3_target_deposition=_delta_roi(
            "step_3_target_deposition",
            steps["step_2"].deposition,
            steps["step_3"].deposition,
            envelope=envelopes["step_3"],
            fallback=target_joint,
            padding=joint_padding,
        ),
        step_4_needle_separation=_delta_roi(
            "step_4_needle_separation",
            steps["step_3"].sample,
            steps["step_4"].sample,
            envelope=envelopes["step_4"],
            fallback=target_joint,
            padding=max(joint_padding, position_padding),
        ),
    )
