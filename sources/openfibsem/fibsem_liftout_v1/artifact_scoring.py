from __future__ import annotations

import binascii
import json
import math
import struct
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from .geometry.artifacts import (
    ArtifactError,
    ArtifactEvidence,
    REQUIRED_COMPONENTS,
    validate_checkpoint_bundle,
)
from .geometry.metrics import Bounds, SceneSnapshot, TriangleMesh, Vec
from .geometry.stl_mesh import CanonicalMesh, MeshEvidence, StlError, parse_stl_path


@dataclass(frozen=True)
class ArtifactCriterion:
    criterion_id: str
    points: float
    maximum_points: float
    metrics: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "criterion_id": self.criterion_id,
            "points": self.points,
            "maximum_points": self.maximum_points,
            "metrics": dict(sorted(self.metrics.items())),
        }


@dataclass(frozen=True)
class CheckpointArtifactScore:
    step_id: str
    points: float
    maximum_points: float
    criteria: Mapping[str, ArtifactCriterion]
    components: Mapping[str, MeshEvidence]
    artifact_evidence: ArtifactEvidence

    def to_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "points": self.points,
            "maximum_points": self.maximum_points,
            "criteria": {
                name: criterion.to_dict()
                for name, criterion in sorted(self.criteria.items())
            },
            "components": {
                name: _mesh_evidence_dict(evidence)
                for name, evidence in sorted(self.components.items())
            },
            "artifact_evidence": self.artifact_evidence.to_dict(),
        }


def _rounded(value: float) -> float:
    return round(value, 6)


def _criterion(
    criterion_id: str,
    points: float,
    maximum: float,
    **metrics: object,
) -> ArtifactCriterion:
    return ArtifactCriterion(
        criterion_id,
        _rounded(min(maximum, max(0.0, points))),
        maximum,
        MappingProxyType(dict(sorted(metrics.items()))),
    )


def _mesh_evidence_dict(evidence: MeshEvidence) -> dict[str, object]:
    return {
        "file_sha256": evidence.file_sha256,
        "canonical_geometry_sha256": evidence.canonical_geometry_sha256,
        "triangle_count": evidence.triangle_count,
        "vertex_count": evidence.vertex_count,
        "connected_component_count": evidence.connected_component_count,
        "watertight": evidence.watertight,
        "non_manifold_edge_count": evidence.non_manifold_edge_count,
        "degenerate_triangle_count": evidence.degenerate_triangle_count,
        "bounds_um": {
            "minimum": list(evidence.bounds_um.minimum),
            "maximum": list(evidence.bounds_um.maximum),
        },
        "volume_um3": _rounded(evidence.volume_um3),
        "surface_area_um2": _rounded(evidence.surface_area_um2),
        "centroid_um": list(evidence.centroid_um),
    }


def _triangle_counter(mesh: TriangleMesh) -> Counter[tuple[Vec, Vec, Vec]]:
    return Counter(tuple(sorted(triangle)) for triangle in mesh.triangles)  # type: ignore[arg-type]


def _scene_matches_components(
    scene: CanonicalMesh | None,
    components: Mapping[str, CanonicalMesh],
) -> bool:
    if scene is None or len(components) != len(REQUIRED_COMPONENTS):
        return False
    expected: Counter[tuple[Vec, Vec, Vec]] = Counter()
    for value in components.values():
        expected.update(_triangle_counter(value.mesh))
    actual = _triangle_counter(scene.mesh)
    return all(actual[triangle] >= count for triangle, count in expected.items())


def _topology_quality(name: str, evidence: MeshEvidence) -> float:
    checks = [
        evidence.watertight,
        evidence.non_manifold_edge_count == 0,
        evidence.degenerate_triangle_count == 0,
    ]
    if name in {"sample.stl", "needle.stl", "target.stl"}:
        checks.append(evidence.connected_component_count == 1)
    return sum(checks) / len(checks)


def _parse_glb(path: Path) -> tuple[dict[str, object], bytes]:
    payload = path.read_bytes()
    if len(payload) < 28:
        raise ValueError("GLB is truncated")
    magic, version, total = struct.unpack_from("<III", payload, 0)
    if magic != 0x46546C67 or version != 2 or total != len(payload):
        raise ValueError("GLB header is invalid")
    json_length, json_type = struct.unpack_from("<II", payload, 12)
    if json_type != 0x4E4F534A or 20 + json_length + 8 > len(payload):
        raise ValueError("GLB JSON chunk is invalid")
    document = json.loads(payload[20 : 20 + json_length].rstrip(b" \0"))
    binary_offset = 20 + json_length
    binary_length, binary_type = struct.unpack_from("<II", payload, binary_offset)
    if binary_type != 0x004E4942 or binary_offset + 8 + binary_length != len(payload):
        raise ValueError("GLB binary chunk is invalid")
    if not isinstance(document, dict):
        raise ValueError("GLB document is invalid")
    return document, payload[binary_offset + 8 :]


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ValueError(f"GLB {name} must be an array")
    return value


def _position_vertices(
    document: Mapping[str, object],
    binary: bytes,
    accessor_index: object,
) -> tuple[Vec, ...]:
    if isinstance(accessor_index, bool) or not isinstance(accessor_index, int):
        raise ValueError("GLB position accessor is invalid")
    accessors = _sequence(document.get("accessors"), "accessors")
    views = _sequence(document.get("bufferViews"), "bufferViews")
    accessor = accessors[accessor_index]
    if not isinstance(accessor, Mapping) or (
        accessor.get("componentType") != 5126 or accessor.get("type") != "VEC3"
    ):
        raise ValueError("GLB position accessor format is invalid")
    count = accessor.get("count")
    view_index = accessor.get("bufferView")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        or isinstance(view_index, bool)
        or not isinstance(view_index, int)
    ):
        raise ValueError("GLB position accessor values are invalid")
    view = views[view_index]
    if not isinstance(view, Mapping) or view.get("buffer", 0) != 0:
        raise ValueError("GLB position buffer view is invalid")
    offset = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    stride = int(view.get("byteStride", 12))
    if stride < 12 or offset < 0 or offset + (count - 1) * stride + 12 > len(binary):
        raise ValueError("GLB position buffer is out of range")
    result = tuple(
        struct.unpack_from("<3f", binary, offset + index * stride)
        for index in range(count)
    )
    if any(not math.isfinite(value) for vertex in result for value in vertex):
        raise ValueError("GLB positions must be finite")
    return result  # type: ignore[return-value]


def _combined_bounds(vertices: Sequence[Vec]) -> Bounds:
    return Bounds(
        tuple(min(vertex[index] for vertex in vertices) for index in range(3)),
        tuple(max(vertex[index] for vertex in vertices) for index in range(3)),
    )  # type: ignore[arg-type]


def _bounds_close(left: Bounds, right: Bounds) -> bool:
    return all(
        abs(actual - expected) <= max(1e-5, abs(expected) * 2e-7)
        for actual, expected in zip(
            (*left.minimum, *left.maximum),
            (*right.minimum, *right.maximum),
            strict=True,
        )
    )


def _glb_matches_components(
    path: Path,
    components: Mapping[str, CanonicalMesh],
) -> tuple[bool, dict[str, object]]:
    try:
        document, binary = _parse_glb(path)
        nodes = _sequence(document.get("nodes"), "nodes")
        meshes = _sequence(document.get("meshes"), "meshes")
        materials = _sequence(document.get("materials"), "materials")
        scenes = _sequence(document.get("scenes"), "scenes")
        if document.get("scene") != 0 or len(scenes) != 1:
            raise ValueError("GLB scene hierarchy is invalid")
        scene = scenes[0]
        if not isinstance(scene, Mapping) or scene.get("nodes") != list(range(len(nodes))):
            raise ValueError("GLB scene node index is invalid")
        material_names = {
            index: material.get("name")
            for index, material in enumerate(materials)
            if isinstance(material, Mapping)
        }
        role_vertices: dict[str, list[Vec]] = {
            role: []
            for role in (
                "source",
                "sample",
                "needle",
                "target",
                "deposition",
                "coupon",
            )
        }
        names: set[str] = set()
        for node in nodes:
            if not isinstance(node, Mapping) or any(
                key in node for key in ("matrix", "translation", "rotation", "scale")
            ):
                raise ValueError("GLB node transform is not canonical")
            name = node.get("name")
            mesh_index = node.get("mesh")
            extras = node.get("extras")
            if (
                not isinstance(name, str)
                or name in names
                or isinstance(mesh_index, bool)
                or not isinstance(mesh_index, int)
                or not isinstance(extras, Mapping)
            ):
                raise ValueError("GLB node identity is invalid")
            names.add(name)
            role = extras.get("role")
            if role not in role_vertices:
                raise ValueError("GLB node role is invalid")
            mesh = meshes[mesh_index]
            if not isinstance(mesh, Mapping):
                raise ValueError("GLB mesh is invalid")
            primitives = _sequence(mesh.get("primitives"), "mesh primitives")
            if len(primitives) != 1 or not isinstance(primitives[0], Mapping):
                raise ValueError("GLB mesh primitive is invalid")
            primitive = primitives[0]
            attributes = primitive.get("attributes")
            if not isinstance(attributes, Mapping) or "POSITION" not in attributes:
                raise ValueError("GLB mesh position is missing")
            if material_names.get(primitive.get("material")) != role:
                raise ValueError("GLB material role is inconsistent")
            role_vertices[str(role)].extend(
                _position_vertices(document, binary, attributes["POSITION"])
            )

        filename_by_role = {
            "source": "source.stl",
            "sample": "sample.stl",
            "needle": "needle.stl",
            "target": "target.stl",
            "deposition": "deposition.stl",
        }
        for role, filename in filename_by_role.items():
            if not role_vertices[role] or filename not in components:
                raise ValueError("GLB component role is incomplete")
            if not _bounds_close(
                _combined_bounds(role_vertices[role]), components[filename].mesh.bounds
            ):
                raise ValueError("GLB component bounds are inconsistent")
        return True, {"node_count": len(nodes), "role_count": len(role_vertices)}
    except (IndexError, KeyError, TypeError, ValueError, struct.error, json.JSONDecodeError) as exc:
        return False, {"error": str(exc)}


def _paeth(left: int, above: int, upper_left: int) -> int:
    prediction = left + above - upper_left
    left_distance = abs(prediction - left)
    above_distance = abs(prediction - above)
    diagonal_distance = abs(prediction - upper_left)
    if left_distance <= above_distance and left_distance <= diagonal_distance:
        return left
    return above if above_distance <= diagonal_distance else upper_left


def _png_pixels(path: Path) -> tuple[int, int, tuple[int, ...]]:
    payload = path.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ArtifactError(f"invalid PNG signature: {path.name}")
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(payload):
        if offset + 12 > len(payload):
            raise ArtifactError(f"truncated PNG chunk: {path.name}")
        length = struct.unpack_from(">I", payload, offset)[0]
        kind = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            raise ArtifactError(f"truncated PNG data: {path.name}")
        data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack_from(">I", payload, offset + 8 + length)[0]
        if binascii.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
            raise ArtifactError(f"PNG CRC mismatch: {path.name}")
        chunks.append((kind, data))
        offset = end
        if kind == b"IEND":
            break
    if not chunks or chunks[0][0] != b"IHDR" or chunks[-1][0] != b"IEND":
        raise ArtifactError(f"PNG chunk order is invalid: {path.name}")
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        raise ArtifactError(f"PNG IHDR is invalid: {path.name}")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if (depth, color, compression, filtering, interlace) != (8, 0, 0, 0, 0):
        raise ArtifactError(f"PNG must be 8-bit non-interlaced grayscale: {path.name}")
    try:
        raw = zlib.decompress(b"".join(data for kind, data in chunks if kind == b"IDAT"))
    except zlib.error as exc:
        raise ArtifactError(f"PNG image data is invalid: {path.name}") from exc
    if len(raw) != height * (width + 1):
        raise ArtifactError(f"PNG scanline size is invalid: {path.name}")
    rows: list[list[int]] = []
    cursor = 0
    for _ in range(height):
        filter_kind = raw[cursor]
        encoded = raw[cursor + 1 : cursor + 1 + width]
        cursor += width + 1
        previous = rows[-1] if rows else [0] * width
        row: list[int] = []
        for index, byte in enumerate(encoded):
            left = row[index - 1] if index else 0
            above = previous[index]
            upper_left = previous[index - 1] if index else 0
            if filter_kind == 0:
                predictor = 0
            elif filter_kind == 1:
                predictor = left
            elif filter_kind == 2:
                predictor = above
            elif filter_kind == 3:
                predictor = (left + above) // 2
            elif filter_kind == 4:
                predictor = _paeth(left, above, upper_left)
            else:
                raise ArtifactError(f"PNG filter is invalid: {path.name}")
            row.append((byte + predictor) & 0xFF)
        rows.append(row)
    return width, height, tuple(value for row in rows for value in row)


def _percentile(sorted_values: Sequence[int], fraction: float) -> float:
    position = fraction * (len(sorted_values) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(sorted_values[low])
    weight = position - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def _image_criteria(
    root: Path,
    beam: str,
    expected_resolution: tuple[int, int],
) -> tuple[ArtifactCriterion, ArtifactCriterion, ArtifactCriterion]:
    width, height, pixels = _png_pixels(root / f"{beam}.png")
    ordered = tuple(sorted(pixels))
    contrast = _percentile(ordered, 0.95) - _percentile(ordered, 0.05)
    constant = ordered[0] == ordered[-1]
    useful_fraction = sum(5 <= value <= 250 for value in pixels) / len(pixels)
    resolution = _criterion(
        f"{beam}_resolution",
        0.125 if (width, height) == expected_resolution else 0.0,
        0.125,
        width=width,
        height=height,
        expected_width=expected_resolution[0],
        expected_height=expected_resolution[1],
        grayscale=True,
    )
    contrast_criterion = _criterion(
        f"{beam}_contrast",
        0.0 if constant else 0.125 * min(1.0, contrast / 32.0),
        0.125,
        robust_contrast=_rounded(contrast),
        constant=constant,
    )
    useful = _criterion(
        f"{beam}_useful_tone",
        0.0 if constant else 0.125 * min(1.0, useful_fraction / 0.10),
        0.125,
        useful_tone_fraction=_rounded(useful_fraction),
        constant=constant,
    )
    return resolution, contrast_criterion, useful


def score_checkpoint_artifacts(
    root: Path,
    expected_world: str,
    expected_step: str,
    *,
    trusted_snapshot: SceneSnapshot | None = None,
    expected_resolution: tuple[int, int] | None = None,
) -> CheckpointArtifactScore:
    """Score evaluator-owned checkpoint files without trusting candidate output."""

    root = Path(root).resolve()
    artifact_evidence = validate_checkpoint_bundle(
        root,
        expected_world=expected_world,
        expected_step=expected_step,
        trusted_snapshot=trusted_snapshot,
    )
    components: dict[str, CanonicalMesh] = {}
    for name in REQUIRED_COMPONENTS:
        try:
            components[name] = parse_stl_path(root / "components" / name)
        except StlError:
            continue
    try:
        scene = parse_stl_path(root / "scene.stl")
    except StlError:
        scene = None

    topology_fraction = (
        sum(_topology_quality(name, value.evidence) for name, value in components.items())
        / len(REQUIRED_COMPONENTS)
    )
    scene_consistent = _scene_matches_components(scene, components)
    criteria: dict[str, ArtifactCriterion] = {
        "stl_topology": _criterion(
            "stl_topology",
            0.75 * topology_fraction,
            0.75,
            valid_component_count=len(components),
            expected_component_count=len(REQUIRED_COMPONENTS),
        ),
        "scene_component_consistency": _criterion(
            "scene_component_consistency",
            0.5 if scene_consistent else 0.0,
            0.5,
            triangle_multiset_equal=scene_consistent,
        ),
    }
    glb_consistent, glb_metrics = _glb_matches_components(
        root / "scene.glb", components
    )
    criteria["glb_consistency"] = _criterion(
        "glb_consistency",
        0.5 if glb_consistent else 0.0,
        0.5,
        **glb_metrics,
    )
    if expected_resolution is None:
        sem_width, sem_height, _ = _png_pixels(root / "sem.png")
        expected_resolution = (sem_width, sem_height)
    if (
        len(expected_resolution) != 2
        or any(isinstance(value, bool) or value < 1 for value in expected_resolution)
    ):
        raise ArtifactError("expected image resolution is invalid")
    for beam in ("sem", "fib"):
        for criterion in _image_criteria(root, beam, expected_resolution):
            criteria[criterion.criterion_id] = criterion
    points = _rounded(sum(criterion.points for criterion in criteria.values()))
    return CheckpointArtifactScore(
        step_id=expected_step,
        points=points,
        maximum_points=2.5,
        criteria=MappingProxyType(dict(sorted(criteria.items()))),
        components=MappingProxyType(
            {name: value.evidence for name, value in sorted(components.items())}
        ),
        artifact_evidence=artifact_evidence,
    )
