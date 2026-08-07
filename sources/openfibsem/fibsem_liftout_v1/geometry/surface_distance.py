from __future__ import annotations

import bisect
import hashlib
import math
import struct
from collections import defaultdict

from .metrics import Vec
from .stl_mesh import CanonicalMesh


MAXIMUM_SURFACE_SAMPLES = 32_768


class SurfaceDistanceError(ValueError):
    """Raised when deterministic surface comparison inputs are invalid."""


def _cross_length(first: Vec, second: Vec, third: Vec) -> float:
    left = tuple(second[index] - first[index] for index in range(3))
    right = tuple(third[index] - first[index] for index in range(3))
    cross = (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )
    return math.sqrt(sum(value * value for value in cross))


def _unit_interval(seed: bytes, counter: int, domain: bytes) -> float:
    digest = hashlib.sha256(seed + domain + struct.pack("<Q", counter)).digest()
    return int.from_bytes(digest[:8], "little") / 2**64


def _surface_samples(value: CanonicalMesh, count: int) -> tuple[Vec, ...]:
    if isinstance(count, bool) or not 1 <= count <= MAXIMUM_SURFACE_SAMPLES:
        raise SurfaceDistanceError("surface sample count exceeds the resource limit")
    areas = tuple(_cross_length(*triangle) / 2.0 for triangle in value.mesh.triangles)
    total_area = sum(areas)
    if total_area <= 0 or not math.isfinite(total_area):
        raise SurfaceDistanceError("surface area must be positive and finite")
    cumulative: list[float] = []
    running = 0.0
    for area in areas:
        running += area
        cumulative.append(running)

    seed = bytes.fromhex(value.evidence.canonical_geometry_sha256)
    result: list[Vec] = []
    for counter in range(count):
        selector = _unit_interval(seed, counter, b"triangle") * total_area
        triangle_index = min(
            bisect.bisect_left(cumulative, selector), len(cumulative) - 1
        )
        first, second, third = value.mesh.triangles[triangle_index]
        u = _unit_interval(seed, counter, b"barycentric-u")
        v = _unit_interval(seed, counter, b"barycentric-v")
        root_u = math.sqrt(u)
        weights = (1.0 - root_u, root_u * (1.0 - v), root_u * v)
        result.append(
            tuple(
                weights[0] * first[axis]
                + weights[1] * second[axis]
                + weights[2] * third[axis]
                for axis in range(3)
            )  # type: ignore[arg-type]
        )
    return tuple(result)


class _SpatialHash:
    def __init__(self, points: tuple[Vec, ...], cell_size: float) -> None:
        if not math.isfinite(cell_size) or cell_size <= 0:
            raise SurfaceDistanceError("surface spatial-hash cell must be positive")
        self.cell_size = cell_size
        cells: dict[tuple[int, int, int], list[Vec]] = defaultdict(list)
        for point in points:
            cells[self._cell(point)].append(point)
        self.cells = dict(cells)
        coordinates = tuple(self.cells)
        self.minimum = tuple(min(cell[axis] for cell in coordinates) for axis in range(3))
        self.maximum = tuple(max(cell[axis] for cell in coordinates) for axis in range(3))

    def _cell(self, point: Vec) -> tuple[int, int, int]:
        return tuple(math.floor(value / self.cell_size) for value in point)  # type: ignore[return-value]

    def nearest_distance(self, point: Vec) -> float:
        origin = self._cell(point)
        maximum_shell = max(
            max(
                abs(origin[axis] - self.minimum[axis]),
                abs(origin[axis] - self.maximum[axis]),
            )
            for axis in range(3)
        )
        best_squared = math.inf
        for shell in range(maximum_shell + 1):
            for dx in range(-shell, shell + 1):
                for dy in range(-shell, shell + 1):
                    for dz in range(-shell, shell + 1):
                        if max(abs(dx), abs(dy), abs(dz)) != shell:
                            continue
                        for candidate in self.cells.get(
                            (origin[0] + dx, origin[1] + dy, origin[2] + dz), ()
                        ):
                            distance_squared = sum(
                                (candidate[axis] - point[axis]) ** 2
                                for axis in range(3)
                            )
                            best_squared = min(best_squared, distance_squared)
            if math.isfinite(best_squared):
                lower_boundary = min(
                    point[axis] - (origin[axis] - shell) * self.cell_size
                    for axis in range(3)
                )
                upper_boundary = min(
                    (origin[axis] + shell + 1) * self.cell_size - point[axis]
                    for axis in range(3)
                )
                unseen_distance = min(lower_boundary, upper_boundary)
                if best_squared <= unseen_distance**2:
                    break
        if not math.isfinite(best_squared):
            raise SurfaceDistanceError("surface spatial hash contains no points")
        return math.sqrt(best_squared)


def _directed_distances(points: tuple[Vec, ...], target: _SpatialHash) -> tuple[float, float]:
    distances = tuple(target.nearest_distance(point) for point in points)
    return sum(distances) / len(distances), max(distances)


def surface_distances(
    candidate: CanonicalMesh,
    reference: CanonicalMesh,
    *,
    sample_count: int,
    cell_size_um: float,
) -> tuple[float, float]:
    """Return bidirectional mean and sampled Hausdorff distances in microns."""

    if (
        candidate.evidence.canonical_geometry_sha256
        == reference.evidence.canonical_geometry_sha256
    ):
        return 0.0, 0.0
    candidate_points = _surface_samples(candidate, sample_count)
    reference_points = _surface_samples(reference, sample_count)
    candidate_index = _SpatialHash(candidate_points, cell_size_um)
    reference_index = _SpatialHash(reference_points, cell_size_um)
    candidate_mean, candidate_maximum = _directed_distances(
        candidate_points, reference_index
    )
    reference_mean, reference_maximum = _directed_distances(
        reference_points, candidate_index
    )
    return (
        (candidate_mean + reference_mean) / 2.0,
        max(candidate_maximum, reference_maximum),
    )
