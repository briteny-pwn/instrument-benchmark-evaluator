from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from sources.openfibsem.fibsem_liftout_v1.geometry.metrics import (
    TriangleMesh,
    box_mesh,
)
from sources.openfibsem.fibsem_liftout_v1.geometry.stl_mesh import (
    StlError,
    StlLimits,
    parse_stl,
    parse_stl_path,
)


def binary_stl(
    mesh: TriangleMesh,
    *,
    faces: tuple[int, ...] | None = None,
) -> bytes:
    triangles = mesh.triangles
    order = tuple(range(len(triangles))) if faces is None else faces
    records = bytearray(b"trusted-test".ljust(80, b"\0"))
    records.extend(struct.pack("<I", len(order)))
    for index in order:
        triangle = triangles[index]
        values = (0.0, 0.0, 0.0, *triangle[0], *triangle[1], *triangle[2])
        records.extend(struct.pack("<12fH", *values, 0))
    return bytes(records)


def ascii_stl(mesh: TriangleMesh, *, reverse: bool = False) -> bytes:
    triangles = mesh.triangles[::-1] if reverse else mesh.triangles
    lines = ["solid trusted"]
    for triangle in triangles:
        lines.extend(("  facet normal 99 99 99", "    outer loop"))
        lines.extend(
            f"      vertex {vertex[0]:.9g} {vertex[1]:.9g} {vertex[2]:.9g}"
            for vertex in triangle
        )
        lines.extend(("    endloop", "  endfacet"))
    lines.append("endsolid trusted")
    return ("\n".join(lines) + "\n").encode("ascii")


def test_binary_and_ascii_box_have_identical_canonical_evidence() -> None:
    mesh = box_mesh(center=(0.0, 0.0, 0.0), size=(2.0, 4.0, 6.0))

    binary = parse_stl(binary_stl(mesh))
    ascii_mesh = parse_stl(ascii_stl(mesh, reverse=True))

    assert (
        binary.evidence.canonical_geometry_sha256
        == ascii_mesh.evidence.canonical_geometry_sha256
    )
    assert binary.evidence.volume_um3 == pytest.approx(48.0)
    assert binary.evidence.surface_area_um2 == pytest.approx(88.0)
    assert binary.evidence.centroid_um == pytest.approx((0.0, 0.0, 0.0))
    assert binary.evidence.watertight
    assert binary.evidence.connected_component_count == 1


def test_triangle_record_order_and_stl_normals_do_not_change_geometry() -> None:
    mesh = box_mesh(center=(3.0, -2.0, 5.0), size=(2.0, 2.0, 2.0))
    forward = parse_stl(binary_stl(mesh))
    reversed_records = parse_stl(
        binary_stl(mesh, faces=tuple(reversed(range(len(mesh.faces)))))
    )

    assert (
        forward.evidence.canonical_geometry_sha256
        == reversed_records.evidence.canonical_geometry_sha256
    )
    assert forward.mesh.vertices == reversed_records.mesh.vertices
    assert forward.mesh.faces == reversed_records.mesh.faces


def test_non_manifold_edge_is_reported_not_silently_repaired() -> None:
    vertices = (
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, -1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    mesh = TriangleMesh(
        vertices=vertices,
        faces=((0, 1, 2), (1, 0, 3), (0, 1, 4)),
    )

    value = parse_stl(binary_stl(mesh))

    assert not value.evidence.watertight
    assert value.evidence.non_manifold_edge_count == 1
    assert value.evidence.connected_component_count == 1


def test_disconnected_solids_are_counted_after_vertex_welding() -> None:
    first = box_mesh(center=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0))
    second = box_mesh(center=(5.0, 0.0, 0.0), size=(1.0, 1.0, 1.0))
    vertices = first.vertices + second.vertices
    offset = len(first.vertices)
    faces = first.faces + tuple(
        tuple(index + offset for index in face) for face in second.faces
    )
    mesh = TriangleMesh(vertices=vertices, faces=faces)

    value = parse_stl(binary_stl(mesh))

    assert value.evidence.connected_component_count == 2
    assert value.evidence.watertight
    assert value.evidence.volume_um3 == pytest.approx(2.0)


def test_degenerate_triangle_is_counted_and_excluded_from_canonical_mesh() -> None:
    good = box_mesh(center=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0))
    payload = bytearray(binary_stl(good))
    count = struct.unpack("<I", payload[80:84])[0]
    payload[80:84] = struct.pack("<I", count + 1)
    payload.extend(
        struct.pack(
            "<12fH",
            0.0,
            0.0,
            0.0,
            2.0,
            2.0,
            2.0,
            2.0,
            2.0,
            2.0,
            2.0,
            2.0,
            2.0,
            0,
        )
    )

    value = parse_stl(bytes(payload))

    assert value.evidence.degenerate_triangle_count == 1
    assert value.evidence.triangle_count == len(good.faces)
    assert value.evidence.volume_um3 == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b"solid broken\nfacet normal 0 0 1\n", "ASCII STL"),
        (b"x" * 80 + struct.pack("<I", 1) + b"short", "STL"),
    ),
)
def test_malformed_stl_is_rejected(payload: bytes, message: str) -> None:
    with pytest.raises(StlError, match=message):
        parse_stl(payload)


def test_non_finite_and_contract_extreme_coordinates_are_rejected() -> None:
    mesh = box_mesh(center=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0))
    payload = bytearray(binary_stl(mesh))
    payload[96:100] = struct.pack("<f", math.inf)
    with pytest.raises(StlError, match="finite"):
        parse_stl(bytes(payload))

    far = box_mesh(center=(1_000_001.0, 0.0, 0.0), size=(1.0, 1.0, 1.0))
    with pytest.raises(StlError, match="coordinate"):
        parse_stl(binary_stl(far))


def test_file_and_triangle_limits_are_enforced(tmp_path: Path) -> None:
    mesh = box_mesh(center=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0))
    payload = binary_stl(mesh)
    path = tmp_path / "box.stl"
    path.write_bytes(payload)

    assert parse_stl_path(path).evidence.file_sha256 == parse_stl(
        payload
    ).evidence.file_sha256
    with pytest.raises(StlError, match="file size"):
        parse_stl(payload, limits=StlLimits(maximum_file_bytes=len(payload) - 1))
    with pytest.raises(StlError, match="triangle"):
        parse_stl(payload, limits=StlLimits(maximum_triangles=1))
