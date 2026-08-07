from __future__ import annotations

import hashlib
import math
import stat
import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .metrics import Bounds, Face, TriangleMesh, Vec


_BINARY_HEADER_BYTES = 84
_BINARY_TRIANGLE_BYTES = 50


class StlError(ValueError):
    """Raised when an STL cannot be accepted as trusted scoring evidence."""


@dataclass(frozen=True)
class StlLimits:
    maximum_file_bytes: int = 64 * 1024 * 1024
    maximum_triangles: int = 1_000_000
    maximum_input_vertices: int = 3_000_000
    maximum_welded_vertices: int = 1_000_000
    maximum_coordinate_abs_um: float = 1_000_000.0

    def __post_init__(self) -> None:
        integer_limits = (
            self.maximum_file_bytes,
            self.maximum_triangles,
            self.maximum_input_vertices,
            self.maximum_welded_vertices,
        )
        if any(isinstance(value, bool) or value <= 0 for value in integer_limits):
            raise ValueError("STL resource limits must be positive integers")
        if not math.isfinite(self.maximum_coordinate_abs_um) or (
            self.maximum_coordinate_abs_um <= 0
        ):
            raise ValueError("STL coordinate limit must be positive and finite")


@dataclass(frozen=True)
class MeshEvidence:
    file_sha256: str
    canonical_geometry_sha256: str
    triangle_count: int
    vertex_count: int
    connected_component_count: int
    watertight: bool
    non_manifold_edge_count: int
    degenerate_triangle_count: int
    bounds_um: Bounds
    volume_um3: float
    surface_area_um2: float
    centroid_um: Vec


@dataclass(frozen=True)
class CanonicalMesh:
    mesh: TriangleMesh
    evidence: MeshEvidence


def _validated_coordinate(value: float, *, limits: StlLimits) -> float:
    if not math.isfinite(value):
        raise StlError("STL coordinates must be finite")
    if abs(value) > limits.maximum_coordinate_abs_um:
        raise StlError("STL coordinate exceeds the scoring contract")
    return value


def _parse_binary(payload: bytes, *, limits: StlLimits) -> list[tuple[Vec, Vec, Vec]]:
    triangle_count = struct.unpack_from("<I", payload, 80)[0]
    if triangle_count > limits.maximum_triangles:
        raise StlError("STL triangle count exceeds the resource limit")
    if triangle_count * 3 > limits.maximum_input_vertices:
        raise StlError("STL input vertex count exceeds the resource limit")

    triangles: list[tuple[Vec, Vec, Vec]] = []
    offset = _BINARY_HEADER_BYTES
    for _ in range(triangle_count):
        record = struct.unpack_from("<12fH", payload, offset)
        vertices = tuple(
            tuple(
                _validated_coordinate(record[3 + vertex * 3 + axis], limits=limits)
                for axis in range(3)
            )
            for vertex in range(3)
        )
        triangles.append(vertices)  # type: ignore[arg-type]
        offset += _BINARY_TRIANGLE_BYTES
    return triangles


def _parse_ascii(payload: bytes, *, limits: StlLimits) -> list[tuple[Vec, Vec, Vec]]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise StlError("invalid STL encoding") from exc

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].lower().startswith("solid"):
        raise StlError("invalid STL: neither canonical binary nor ASCII STL")
    if not lines[-1].lower().startswith("endsolid"):
        raise StlError("invalid ASCII STL: missing endsolid")

    triangles: list[tuple[Vec, Vec, Vec]] = []
    index = 1
    try:
        while index < len(lines) - 1:
            facet = lines[index].split()
            if len(facet) != 5 or [part.lower() for part in facet[:2]] != [
                "facet",
                "normal",
            ]:
                raise StlError("invalid ASCII STL facet")
            for value in facet[2:]:
                if not math.isfinite(float(value)):
                    raise StlError("ASCII STL normals must be finite")
            index += 1

            if index >= len(lines) - 1 or lines[index].lower() != "outer loop":
                raise StlError("invalid ASCII STL outer loop")
            index += 1

            vertices: list[Vec] = []
            for _ in range(3):
                if index >= len(lines) - 1:
                    raise StlError("invalid ASCII STL vertex list")
                fields = lines[index].split()
                if len(fields) != 4 or fields[0].lower() != "vertex":
                    raise StlError("invalid ASCII STL vertex")
                vertices.append(
                    tuple(
                        _validated_coordinate(float(value), limits=limits)
                        for value in fields[1:]
                    )  # type: ignore[arg-type]
                )
                index += 1

            if index >= len(lines) - 1 or lines[index].lower() != "endloop":
                raise StlError("invalid ASCII STL endloop")
            index += 1
            if index >= len(lines) - 1 or lines[index].lower() != "endfacet":
                raise StlError("invalid ASCII STL endfacet")
            index += 1

            triangles.append(tuple(vertices))  # type: ignore[arg-type]
            if len(triangles) > limits.maximum_triangles:
                raise StlError("ASCII STL triangle count exceeds the resource limit")
            if len(triangles) * 3 > limits.maximum_input_vertices:
                raise StlError("ASCII STL input vertex count exceeds the resource limit")
    except ValueError as exc:
        if isinstance(exc, StlError):
            raise
        raise StlError("invalid numeric value in ASCII STL") from exc

    if not triangles:
        raise StlError("invalid ASCII STL: no triangles")
    return triangles


def _weld_index(value: float, epsilon: float) -> int:
    scaled = value / epsilon
    if scaled >= 0:
        return int(math.floor(scaled + 0.5))
    return int(math.ceil(scaled - 0.5))


def _canonical_cycle(face: Face) -> Face:
    rotations = (face, (face[1], face[2], face[0]), (face[2], face[0], face[1]))
    return min(rotations)


def _cross_length(first: Vec, second: Vec, third: Vec) -> float:
    left = tuple(second[index] - first[index] for index in range(3))
    right = tuple(third[index] - first[index] for index in range(3))
    cross = (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )
    return math.sqrt(sum(value * value for value in cross))


def _component_count(vertex_count: int, faces: tuple[Face, ...]) -> int:
    parent = list(range(vertex_count))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for first, second, third in faces:
        union(first, second)
        union(second, third)
    return len({find(index) for index in range(vertex_count)})


def _canonical_digest(mesh: TriangleMesh) -> str:
    value = hashlib.sha256()
    value.update(struct.pack("<Q", len(mesh.vertices)))
    for vertex in mesh.vertices:
        value.update(struct.pack("<3d", *vertex))
    value.update(struct.pack("<Q", len(mesh.faces)))
    for face in mesh.faces:
        value.update(struct.pack("<3Q", *face))
    return value.hexdigest()


def parse_stl(
    payload: bytes,
    *,
    limits: StlLimits = StlLimits(),
    weld_epsilon_um: float = 1e-6,
) -> CanonicalMesh:
    """Parse ASCII or binary STL into deterministic, welded scoring evidence."""

    if not isinstance(payload, bytes):
        raise TypeError("STL payload must be bytes")
    if not payload:
        raise StlError("STL file is empty")
    if len(payload) > limits.maximum_file_bytes:
        raise StlError("STL file size exceeds the resource limit")
    if not math.isfinite(weld_epsilon_um) or weld_epsilon_um <= 0:
        raise ValueError("STL weld epsilon must be positive and finite")

    is_binary = False
    if len(payload) >= _BINARY_HEADER_BYTES:
        declared = struct.unpack_from("<I", payload, 80)[0]
        is_binary = _BINARY_HEADER_BYTES + declared * _BINARY_TRIANGLE_BYTES == len(
            payload
        )
    triangles = (
        _parse_binary(payload, limits=limits)
        if is_binary
        else _parse_ascii(payload, limits=limits)
    )

    welded_coordinates: dict[tuple[int, int, int], Vec] = {}
    keyed_triangles: list[tuple[tuple[int, int, int], ...]] = []
    for triangle in triangles:
        keys: list[tuple[int, int, int]] = []
        for vertex in triangle:
            key = tuple(_weld_index(value, weld_epsilon_um) for value in vertex)
            coordinate = tuple(
                0.0 if index == 0 else index * weld_epsilon_um for index in key
            )
            welded_coordinates.setdefault(key, coordinate)  # type: ignore[arg-type]
            keys.append(key)  # type: ignore[arg-type]
        keyed_triangles.append(tuple(keys))

    if len(welded_coordinates) > limits.maximum_welded_vertices:
        raise StlError("STL welded vertex count exceeds the resource limit")

    valid_keyed_faces: list[tuple[tuple[int, int, int], ...]] = []
    degenerate_count = 0
    for keys in keyed_triangles:
        points = tuple(welded_coordinates[key] for key in keys)
        if len(set(keys)) != 3 or _cross_length(*points) <= weld_epsilon_um**2:
            degenerate_count += 1
            continue
        valid_keyed_faces.append(keys)
    if not valid_keyed_faces:
        raise StlError("STL contains no non-degenerate triangles")

    used_keys = sorted({key for face in valid_keyed_faces for key in face})
    key_to_index = {key: index for index, key in enumerate(used_keys)}
    vertices = tuple(welded_coordinates[key] for key in used_keys)
    faces = tuple(
        sorted(
            _canonical_cycle(tuple(key_to_index[key] for key in face))
            for face in valid_keyed_faces
        )
    )
    mesh = TriangleMesh(vertices=vertices, faces=faces)

    edge_counts: Counter[tuple[int, int]] = Counter()
    for first, second, third in faces:
        edge_counts.update(
            (
                tuple(sorted((first, second))),
                tuple(sorted((second, third))),
                tuple(sorted((third, first))),
            )
        )
    non_manifold_edge_count = sum(count > 2 for count in edge_counts.values())
    watertight = bool(edge_counts) and all(count == 2 for count in edge_counts.values())
    surface_area = sum(_cross_length(*triangle) / 2.0 for triangle in mesh.triangles)

    evidence = MeshEvidence(
        file_sha256=hashlib.sha256(payload).hexdigest(),
        canonical_geometry_sha256=_canonical_digest(mesh),
        triangle_count=len(faces),
        vertex_count=len(vertices),
        connected_component_count=_component_count(len(vertices), faces),
        watertight=watertight,
        non_manifold_edge_count=non_manifold_edge_count,
        degenerate_triangle_count=degenerate_count,
        bounds_um=mesh.bounds,
        volume_um3=mesh.volume_um3,
        surface_area_um2=surface_area,
        centroid_um=mesh.centroid,
    )
    return CanonicalMesh(mesh=mesh, evidence=evidence)


def parse_stl_path(
    path: Path,
    *,
    limits: StlLimits = StlLimits(),
    weld_epsilon_um: float = 1e-6,
) -> CanonicalMesh:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise StlError(f"cannot inspect STL file: {path.name}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StlError("STL path must be a regular file, not a link")
    if metadata.st_size > limits.maximum_file_bytes:
        raise StlError("STL file size exceeds the resource limit")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise StlError(f"cannot read STL file: {path.name}") from exc
    return parse_stl(payload, limits=limits, weld_epsilon_um=weld_epsilon_um)
