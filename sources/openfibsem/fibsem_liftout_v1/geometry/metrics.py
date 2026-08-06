from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from ..models import finite, vec3


Vec = tuple[float, float, float]
Face = tuple[int, int, int]


def _sub(left: Vec, right: Vec) -> Vec:
    return tuple(a - b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _dot(left: Vec, right: Vec) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(left: Vec, right: Vec) -> Vec:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


@dataclass(frozen=True)
class Bounds:
    minimum: Vec
    maximum: Vec

    def __post_init__(self) -> None:
        if any(high < low for low, high in zip(self.minimum, self.maximum, strict=True)):
            raise ValueError("bounds maximum is below minimum")

    @property
    def size(self) -> Vec:
        return _sub(self.maximum, self.minimum)

    def overlaps(self, other: "Bounds", padding: float = 0.0) -> bool:
        return all(
            self.minimum[index] <= other.maximum[index] + padding
            and other.minimum[index] <= self.maximum[index] + padding
            for index in range(3)
        )

    def overlap_extents(self, other: "Bounds") -> Vec:
        return tuple(
            max(
                0.0,
                min(self.maximum[index], other.maximum[index])
                - max(self.minimum[index], other.minimum[index]),
            )
            for index in range(3)
        )  # type: ignore[return-value]

    def contains(self, other: "Bounds", tolerance: float = 1e-9) -> bool:
        return all(
            self.minimum[index] - tolerance <= other.minimum[index]
            and other.maximum[index] <= self.maximum[index] + tolerance
            for index in range(3)
        )


@dataclass(frozen=True)
class TriangleMesh:
    vertices: tuple[Vec, ...]
    faces: tuple[Face, ...]

    def __post_init__(self) -> None:
        normalized_vertices = tuple(vec3(vertex, "mesh vertex") for vertex in self.vertices)
        normalized_faces: list[Face] = []
        if len(normalized_vertices) < 3 or not self.faces:
            raise ValueError("mesh must contain vertices and faces")
        for face in self.faces:
            if len(face) != 3 or any(
                isinstance(index, bool) or not isinstance(index, int) for index in face
            ):
                raise ValueError("mesh faces must contain three integer indices")
            if min(face) < 0 or max(face) >= len(normalized_vertices):
                raise ValueError("mesh face index is out of range")
            if len(set(face)) != 3:
                raise ValueError("mesh face is degenerate")
            normalized_faces.append(tuple(face))  # type: ignore[arg-type]
        object.__setattr__(self, "vertices", normalized_vertices)
        object.__setattr__(self, "faces", tuple(normalized_faces))

    @property
    def bounds(self) -> Bounds:
        return Bounds(
            tuple(min(vertex[index] for vertex in self.vertices) for index in range(3)),  # type: ignore[arg-type]
            tuple(max(vertex[index] for vertex in self.vertices) for index in range(3)),  # type: ignore[arg-type]
        )

    @property
    def volume_um3(self) -> float:
        signed = 0.0
        for first, second, third in self.triangles:
            signed += _dot(first, _cross(second, third)) / 6.0
        return abs(signed)

    @property
    def centroid(self) -> Vec:
        return tuple(
            sum(vertex[index] for vertex in self.vertices) / len(self.vertices)
            for index in range(3)
        )  # type: ignore[return-value]

    @property
    def triangles(self) -> tuple[tuple[Vec, Vec, Vec], ...]:
        return tuple(
            (self.vertices[face[0]], self.vertices[face[1]], self.vertices[face[2]])
            for face in self.faces
        )


def box_mesh(*, center: Sequence[float], size: Sequence[float]) -> TriangleMesh:
    cx, cy, cz = vec3(center, "box center")
    sx, sy, sz = vec3(size, "box size")
    if min(sx, sy, sz) <= 0:
        raise ValueError("box size must be positive")
    x0, x1 = cx - sx / 2, cx + sx / 2
    y0, y1 = cy - sy / 2, cy + sy / 2
    z0, z1 = cz - sz / 2, cz + sz / 2
    vertices: tuple[Vec, ...] = (
        (x0, y0, z0),
        (x1, y0, z0),
        (x1, y1, z0),
        (x0, y1, z0),
        (x0, y0, z1),
        (x1, y0, z1),
        (x1, y1, z1),
        (x0, y1, z1),
    )
    faces: tuple[Face, ...] = (
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (3, 7, 6),
        (3, 6, 2),
        (0, 4, 7),
        (0, 7, 3),
        (1, 2, 6),
        (1, 6, 5),
    )
    return TriangleMesh(vertices, faces)


@dataclass(frozen=True)
class MeshPart:
    name: str
    role: str
    mesh: TriangleMesh
    purpose: str | None = None

    def __post_init__(self) -> None:
        if not self.name or self.role not in {
            "source",
            "sample",
            "needle",
            "target",
            "deposition",
            "coupon",
        }:
            raise ValueError("mesh part identity is invalid")
        if self.role == "deposition" and not self.purpose:
            raise ValueError("deposition mesh requires a purpose")


@dataclass(frozen=True)
class PoseState:
    position_um: Vec
    orientation_degrees: Vec

    def __post_init__(self) -> None:
        object.__setattr__(self, "position_um", vec3(self.position_um, "pose position"))
        object.__setattr__(
            self,
            "orientation_degrees",
            vec3(self.orientation_degrees, "pose orientation"),
        )


@dataclass(frozen=True)
class MaterialChange:
    operation: str
    purpose: str
    bounds: Bounds
    signed_volume_um3: float

    def __post_init__(self) -> None:
        if self.operation not in {"cut", "deposition"}:
            raise ValueError("material change operation is invalid")
        volume = finite(self.signed_volume_um3, "signed material volume")
        if (self.operation == "cut" and volume >= 0) or (
            self.operation == "deposition" and volume <= 0
        ):
            raise ValueError("material change signed volume is invalid")
        object.__setattr__(self, "signed_volume_um3", volume)


@dataclass(frozen=True)
class SceneSnapshot:
    checkpoint_id: str
    parts: tuple[MeshPart, ...]
    poses: Mapping[str, PoseState]
    planned_sample_volume_um3: float
    material_changes: tuple[MaterialChange, ...]
    needle_inserted: bool
    active_operation: bool
    collision: bool

    def __post_init__(self) -> None:
        if self.checkpoint_id not in {"step_1", "step_2", "step_3", "step_4"}:
            raise ValueError("checkpoint ID is invalid")
        if not self.parts or len({part.name for part in self.parts}) != len(self.parts):
            raise ValueError("scene part names must be non-empty and unique")
        planned = finite(self.planned_sample_volume_um3, "planned sample volume")
        if planned <= 0:
            raise ValueError("planned sample volume must be positive")
        object.__setattr__(self, "planned_sample_volume_um3", planned)

    def to_init_dict(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "parts": self.parts,
            "poses": self.poses,
            "planned_sample_volume_um3": self.planned_sample_volume_um3,
            "material_changes": self.material_changes,
            "needle_inserted": self.needle_inserted,
            "active_operation": self.active_operation,
            "collision": self.collision,
        }
