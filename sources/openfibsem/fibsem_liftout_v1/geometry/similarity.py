from __future__ import annotations

import math
from dataclasses import dataclass

from .metrics import Bounds
from .stl_mesh import CanonicalMesh
from .surface_distance import SurfaceDistanceError, surface_distances
from .voxel import VoxelError, voxel_iou


@dataclass(frozen=True)
class ShapeParameters:
    voxel_size_um: float
    surface_sample_count: int


@dataclass(frozen=True)
class ShapeComparison:
    candidate_volume_um3: float
    reference_volume_um3: float
    volume_similarity: float
    voxel_iou: float
    symmetric_surface_distance_um: float
    hausdorff_distance_um: float
    asd_score: float
    hausdorff_score: float
    shape_score: float
    voxel_size_um: float
    surface_sample_count: int
    candidate_geometry_sha256: str
    reference_geometry_sha256: str


class ShapeMetricError(ValueError):
    """Raised when a shape metric violates the evaluator contract."""


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ShapeMetricError(f"{name} must be positive and finite")
    return value


def adaptive_shape_parameters(
    *,
    characteristic_length_um: float,
    surface_area_um2: float,
) -> ShapeParameters:
    characteristic_length_um = _positive_finite(
        characteristic_length_um, "characteristic length"
    )
    if isinstance(surface_area_um2, bool) or not math.isfinite(surface_area_um2):
        raise ShapeMetricError("surface area must be finite")
    if surface_area_um2 < 0:
        raise ShapeMetricError("surface area must be non-negative")
    voxel_size = min(0.5, max(0.1, 0.02 * characteristic_length_um))
    sample_count = min(
        32_768,
        max(2_048, math.ceil(surface_area_um2 / voxel_size**2)),
    )
    return ShapeParameters(
        voxel_size_um=round(voxel_size, 6),
        surface_sample_count=sample_count,
    )


def _rounded(value: float) -> float:
    return round(value, 6)


def compare_shapes(
    candidate: CanonicalMesh,
    reference: CanonicalMesh,
    roi: Bounds,
    *,
    tau_um: float,
    characteristic_length_um: float,
) -> ShapeComparison:
    tau_um = _positive_finite(tau_um, "shape tolerance")
    parameters = adaptive_shape_parameters(
        characteristic_length_um=characteristic_length_um,
        surface_area_um2=max(
            candidate.evidence.surface_area_um2,
            reference.evidence.surface_area_um2,
        ),
    )
    candidate_volume = candidate.evidence.volume_um3
    reference_volume = reference.evidence.volume_um3
    largest_volume = max(candidate_volume, reference_volume)
    volume_similarity = (
        min(candidate_volume, reference_volume) / largest_volume
        if largest_volume > 0
        else 0.0
    )
    try:
        occupancy_iou = voxel_iou(
            candidate.mesh,
            reference.mesh,
            roi,
            parameters.voxel_size_um,
        )
        mean_distance, maximum_distance = surface_distances(
            candidate,
            reference,
            sample_count=parameters.surface_sample_count,
            cell_size_um=max(parameters.voxel_size_um, tau_um),
        )
    except (VoxelError, SurfaceDistanceError) as exc:
        raise ShapeMetricError(str(exc)) from exc

    asd_score = math.exp(-((mean_distance / tau_um) ** 2))
    hausdorff_score = min(1.0, max(0.0, 1.0 - maximum_distance / (3 * tau_um)))
    shape_score = (
        0.25 * volume_similarity
        + 0.35 * occupancy_iou
        + 0.25 * asd_score
        + 0.15 * hausdorff_score
    )
    return ShapeComparison(
        candidate_volume_um3=_rounded(candidate_volume),
        reference_volume_um3=_rounded(reference_volume),
        volume_similarity=_rounded(volume_similarity),
        voxel_iou=_rounded(occupancy_iou),
        symmetric_surface_distance_um=_rounded(mean_distance),
        hausdorff_distance_um=_rounded(maximum_distance),
        asd_score=_rounded(asd_score),
        hausdorff_score=_rounded(hausdorff_score),
        shape_score=_rounded(shape_score),
        voxel_size_um=parameters.voxel_size_um,
        surface_sample_count=parameters.surface_sample_count,
        candidate_geometry_sha256=candidate.evidence.canonical_geometry_sha256,
        reference_geometry_sha256=reference.evidence.canonical_geometry_sha256,
    )
