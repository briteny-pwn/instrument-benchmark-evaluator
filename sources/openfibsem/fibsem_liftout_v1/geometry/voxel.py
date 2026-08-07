from __future__ import annotations

import bisect
import math
from collections import defaultdict

from .metrics import Bounds, TriangleMesh, Vec


MAXIMUM_VOXEL_CELLS = 4_194_304


class VoxelError(ValueError):
    """Raised when a deterministic voxel comparison cannot be evaluated."""


def _cell_center(low: float, high: float, index: int, edge: float) -> float:
    cell_low = low + index * edge
    return (cell_low + min(cell_low + edge, high)) / 2.0


def _grid_shape(roi: Bounds, edge: float, maximum_cells: int) -> tuple[int, int, int]:
    if not math.isfinite(edge) or edge <= 0:
        raise VoxelError("voxel size must be positive and finite")
    if isinstance(maximum_cells, bool) or maximum_cells <= 0:
        raise VoxelError("maximum voxel cell count must be positive")
    if any(not math.isfinite(value) for value in (*roi.minimum, *roi.maximum)):
        raise VoxelError("voxel ROI bounds must be finite")
    if any(extent <= 0 for extent in roi.size):
        raise VoxelError("voxel ROI must have positive volume")
    shape = tuple(max(1, math.ceil(extent / edge)) for extent in roi.size)
    if math.prod(shape) > maximum_cells:
        raise VoxelError("voxel cell count exceeds the resource limit")
    return shape  # type: ignore[return-value]


def _candidate_index_range(
    bound_low: float,
    bound_high: float,
    roi_low: float,
    count: int,
    edge: float,
) -> range:
    first = max(0, math.floor((bound_low - roi_low) / edge) - 1)
    last = min(count - 1, math.floor((bound_high - roi_low) / edge) + 1)
    return range(first, last + 1)


def _projected_intersections(
    mesh: TriangleMesh,
    roi: Bounds,
    edge: float,
    shape: tuple[int, int, int],
) -> dict[tuple[int, int], list[float]]:
    _, ny, nz = shape
    rows: dict[tuple[int, int], list[float]] = defaultdict(list)
    epsilon = max(edge * 1e-10, 1e-12)
    for first, second, third in mesh.triangles:
        y0, z0 = first[1], first[2]
        y1, z1 = second[1], second[2]
        y2, z2 = third[1], third[2]
        denominator = (y1 - y2) * (z0 - z2) + (z2 - z1) * (y0 - y2)
        if abs(denominator) <= epsilon:
            continue
        y_range = _candidate_index_range(
            min(y0, y1, y2), max(y0, y1, y2), roi.minimum[1], ny, edge
        )
        z_range = _candidate_index_range(
            min(z0, z1, z2), max(z0, z1, z2), roi.minimum[2], nz, edge
        )
        for iz in z_range:
            z_value = _cell_center(
                roi.minimum[2], roi.maximum[2], iz, edge
            )
            if z_value < min(z0, z1, z2) - epsilon or (
                z_value > max(z0, z1, z2) + epsilon
            ):
                continue
            for iy in y_range:
                y_value = _cell_center(
                    roi.minimum[1], roi.maximum[1], iy, edge
                )
                if y_value < min(y0, y1, y2) - epsilon or (
                    y_value > max(y0, y1, y2) + epsilon
                ):
                    continue
                first_weight = (
                    (y1 - y2) * (z_value - z2)
                    + (z2 - z1) * (y_value - y2)
                ) / denominator
                second_weight = (
                    (y2 - y0) * (z_value - z2)
                    + (z0 - z2) * (y_value - y2)
                ) / denominator
                third_weight = 1.0 - first_weight - second_weight
                if min(first_weight, second_weight, third_weight) < -epsilon:
                    continue
                if max(first_weight, second_weight, third_weight) > 1.0 + epsilon:
                    continue
                x_value = (
                    first_weight * first[0]
                    + second_weight * second[0]
                    + third_weight * third[0]
                )
                rows[(iy, iz)].append(x_value)
    return rows


def _voxelize(
    mesh: TriangleMesh,
    roi: Bounds,
    edge: float,
    shape: tuple[int, int, int],
) -> set[int]:
    nx, ny, _ = shape
    occupied: set[int] = set()
    epsilon = max(edge * 1e-9, 1e-12)
    for (iy, iz), raw_intersections in _projected_intersections(
        mesh, roi, edge, shape
    ).items():
        intersections: list[float] = []
        for value in sorted(raw_intersections):
            if not intersections or abs(value - intersections[-1]) > epsilon:
                intersections.append(value)
        for ix in range(nx):
            x_value = _cell_center(
                roi.minimum[0], roi.maximum[0], ix, edge
            )
            after = len(intersections) - bisect.bisect_right(
                intersections, x_value + epsilon
            )
            if after % 2:
                occupied.add((iz * ny + iy) * nx + ix)
    return occupied


def voxel_iou(
    candidate: TriangleMesh,
    reference: TriangleMesh,
    roi: Bounds,
    voxel_size_um: float,
    *,
    maximum_cells: int = MAXIMUM_VOXEL_CELLS,
) -> float:
    """Return deterministic occupancy IoU on a reference-fixed ROI grid."""

    shape = _grid_shape(roi, voxel_size_um, maximum_cells)
    candidate_cells = _voxelize(candidate, roi, voxel_size_um, shape)
    reference_cells = _voxelize(reference, roi, voxel_size_um, shape)
    union = candidate_cells | reference_cells
    if not union:
        return 1.0 if not candidate_cells and not reference_cells else 0.0
    return len(candidate_cells & reference_cells) / len(union)
