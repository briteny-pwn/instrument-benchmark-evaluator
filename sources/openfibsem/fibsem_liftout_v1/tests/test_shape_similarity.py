from __future__ import annotations

import struct

import pytest

from sources.openfibsem.fibsem_liftout_v1.geometry.metrics import (
    Bounds,
    TriangleMesh,
    box_mesh,
)
from sources.openfibsem.fibsem_liftout_v1.geometry.similarity import (
    ShapeMetricError,
    adaptive_shape_parameters,
    compare_shapes,
)
from sources.openfibsem.fibsem_liftout_v1.geometry.stl_mesh import parse_stl


def canonical_mesh(mesh: TriangleMesh, *, reverse: bool = False):
    triangles = mesh.triangles[::-1] if reverse else mesh.triangles
    value = bytearray(b"shape-test".ljust(80, b"\0"))
    value.extend(struct.pack("<I", len(triangles)))
    for triangle in triangles:
        value.extend(
            struct.pack(
                "<12fH",
                0.0,
                0.0,
                0.0,
                *triangle[0],
                *triangle[1],
                *triangle[2],
                0,
            )
        )
    return parse_stl(bytes(value))


def comparison(candidate: TriangleMesh, reference: TriangleMesh):
    return compare_shapes(
        canonical_mesh(candidate),
        canonical_mesh(reference),
        Bounds((-5.0, -5.0, -5.0), (5.0, 5.0, 5.0)),
        tau_um=0.5,
        characteristic_length_um=4.0,
    )


def test_identical_box_scores_one() -> None:
    box = box_mesh(center=(0.0, 0.0, 0.0), size=(2.0, 4.0, 6.0))

    result = comparison(box, box)

    assert result.shape_score == 1.0
    assert result.volume_similarity == 1.0
    assert result.voxel_iou == 1.0
    assert result.symmetric_surface_distance_um == 0.0
    assert result.hausdorff_distance_um == 0.0
    assert result.voxel_size_um == 0.1
    assert 2_048 <= result.surface_sample_count <= 32_768


def test_equal_volume_different_shape_loses_shape_points() -> None:
    tall = box_mesh(center=(0.0, 0.0, 0.0), size=(2.0, 2.0, 8.0))
    flat = box_mesh(center=(0.0, 0.0, 0.0), size=(4.0, 4.0, 2.0))

    result = comparison(tall, flat)

    assert result.candidate_volume_um3 == pytest.approx(32.0)
    assert result.reference_volume_um3 == pytest.approx(32.0)
    assert result.volume_similarity == 1.0
    assert result.voxel_iou < 0.2
    assert result.shape_score < 0.8


def test_triangle_record_order_does_not_change_metrics() -> None:
    candidate = box_mesh(center=(0.25, -0.5, 0.75), size=(2.0, 3.0, 4.0))
    reference = canonical_mesh(
        box_mesh(center=(0.0, 0.0, 0.0), size=(2.0, 3.0, 4.0))
    )
    roi = Bounds((-4.0, -4.0, -4.0), (4.0, 4.0, 4.0))

    forward = compare_shapes(
        canonical_mesh(candidate),
        reference,
        roi,
        tau_um=0.5,
        characteristic_length_um=3.0,
    )
    reverse = compare_shapes(
        canonical_mesh(candidate, reverse=True),
        reference,
        roi,
        tau_um=0.5,
        characteristic_length_um=3.0,
    )

    assert forward == reverse


def test_adaptive_parameters_apply_public_clamps() -> None:
    minimum = adaptive_shape_parameters(
        characteristic_length_um=1.0,
        surface_area_um2=0.01,
    )
    maximum = adaptive_shape_parameters(
        characteristic_length_um=100.0,
        surface_area_um2=1_000_000.0,
    )

    assert minimum.voxel_size_um == 0.1
    assert minimum.surface_sample_count == 2_048
    assert maximum.voxel_size_um == 0.5
    assert maximum.surface_sample_count == 32_768


def test_voxel_cell_limit_rejects_oversized_roi() -> None:
    box = canonical_mesh(
        box_mesh(center=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0))
    )
    with pytest.raises(ShapeMetricError, match="voxel cell"):
        compare_shapes(
            box,
            box,
            Bounds((-50.0, -50.0, -50.0), (50.0, 50.0, 50.0)),
            tau_um=0.5,
            characteristic_length_um=1.0,
        )


@pytest.mark.parametrize(
    ("tau_um", "characteristic_length_um"),
    ((0.0, 1.0), (1.0, 0.0), (float("nan"), 1.0)),
)
def test_invalid_metric_scales_are_rejected(
    tau_um: float,
    characteristic_length_um: float,
) -> None:
    box = canonical_mesh(
        box_mesh(center=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0))
    )
    with pytest.raises(ShapeMetricError):
        compare_shapes(
            box,
            box,
            Bounds((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)),
            tau_um=tau_um,
            characteristic_length_um=characteristic_length_um,
        )
