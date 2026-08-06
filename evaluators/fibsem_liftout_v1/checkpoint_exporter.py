from __future__ import annotations

import binascii
import hashlib
import json
import math
import os
import shutil
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Mapping

from .geometry.artifacts import (
    REQUIRED_COMPONENTS,
    ArtifactEvidence,
    validate_checkpoint_bundle,
)
from .geometry.metrics import MeshPart, SceneSnapshot, TriangleMesh
from .geometry.oracle import _canonical_geometry_hash


ROLE_COMPONENT = {
    "source.stl": {"source"},
    "sample.stl": {"sample"},
    "needle.stl": {"needle"},
    "target.stl": {"target"},
    "deposition.stl": {"deposition"},
}
COLORS = {
    "source": (0.38, 0.42, 0.48, 1.0),
    "sample": (0.20, 0.70, 0.95, 1.0),
    "needle": (0.92, 0.74, 0.18, 1.0),
    "target": (0.35, 0.78, 0.42, 1.0),
    "deposition": (0.86, 0.35, 0.72, 1.0),
    "coupon": (0.55, 0.40, 0.28, 1.0),
}


class CheckpointExporter:
    def __init__(self, evidence_root: Path):
        self.evidence_root = Path(evidence_root).resolve()
        self.evidence_root.mkdir(parents=True, exist_ok=True)

    def export(
        self,
        snapshot: SceneSnapshot,
        images: Mapping[str, tuple[int, int, bytes]],
        *,
        world_id: str,
        journal_sequence: int,
        journal_hash: str,
        scenario_digest: str | None = None,
        geometry_metrics: Mapping[str, object] | None = None,
    ) -> ArtifactEvidence:
        if set(images) != {"SEM", "FIB"}:
            raise ValueError("checkpoint images must contain SEM and FIB")
        if not world_id or "/" in world_id or world_id in {".", ".."}:
            raise ValueError("world ID is invalid")
        if journal_sequence < 1:
            raise ValueError("journal sequence is invalid")
        _digest(journal_hash, "journal")
        if (scenario_digest is None) != (geometry_metrics is None):
            raise ValueError(
                "scenario digest and geometry metrics must be supplied together"
            )
        if scenario_digest is not None:
            _digest(scenario_digest, "scenario")
        destination = self.evidence_root / "artifacts" / world_id / snapshot.checkpoint_id
        if destination.exists():
            raise FileExistsError(f"checkpoint already exists: {snapshot.checkpoint_id}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{snapshot.checkpoint_id}.", suffix=".tmp", dir=destination.parent
            )
        )
        try:
            components = temporary / "components"
            components.mkdir()
            payloads: dict[str, bytes] = {
                "scene.glb": _glb(snapshot.parts),
                "scene.stl": _stl(snapshot.parts),
            }
            for filename in REQUIRED_COMPONENTS:
                roles = ROLE_COMPONENT[filename]
                selected = tuple(part for part in snapshot.parts if part.role in roles)
                if not selected:
                    raise ValueError(f"trusted snapshot has no {filename} component")
                payloads[f"components/{filename}"] = _stl(selected)
            for beam, filename in (("SEM", "sem.png"), ("FIB", "fib.png")):
                width, height, pixels = images[beam]
                payloads[filename] = _png(width, height, pixels)
            for relative, payload in payloads.items():
                path = temporary / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_fsync(path, payload)
            geometry_hash = _canonical_geometry_hash(snapshot.parts)
            checkpoint = {
                "schema_version": 1,
                "world_id": world_id,
                "step_id": snapshot.checkpoint_id,
                "journal_sequence": journal_sequence,
                "journal_hash": journal_hash,
                "geometry_hash": geometry_hash,
                "artifacts": {
                    relative: {
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                    for relative, payload in sorted(payloads.items())
                },
            }
            if scenario_digest is not None and geometry_metrics is not None:
                if geometry_metrics.get("canonical_geometry_hash") != geometry_hash:
                    raise ValueError("geometry metrics do not match trusted snapshot")
                checkpoint["scenario_digest"] = scenario_digest
                checkpoint["geometry"] = dict(geometry_metrics)
            _write_fsync(
                temporary / "checkpoint.json",
                json.dumps(
                    checkpoint,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("ascii")
                + b"\n",
            )
            _fsync_directory(components)
            _fsync_directory(temporary)
            evidence = validate_checkpoint_bundle(
                temporary,
                expected_world=world_id,
                expected_step=snapshot.checkpoint_id,
                trusted_snapshot=snapshot,
            )
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
            return validate_checkpoint_bundle(
                destination,
                expected_world=world_id,
                expected_step=snapshot.checkpoint_id,
                trusted_snapshot=snapshot,
            )
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def _stl(parts: tuple[MeshPart, ...]) -> bytes:
    triangles = [triangle for part in parts for triangle in part.mesh.triangles]
    if not triangles:
        raise ValueError("STL requires at least one triangle")
    output = bytearray(b"FIBSEM IAB trusted checkpoint".ljust(80, b"\0"))
    output.extend(struct.pack("<I", len(triangles)))
    for first, second, third in triangles:
        normal = _normal(first, second, third)
        output.extend(
            struct.pack(
                "<12fH",
                *normal,
                *first,
                *second,
                *third,
                0,
            )
        )
    return bytes(output)


def _normal(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
    third: tuple[float, float, float],
) -> tuple[float, float, float]:
    left = tuple(b - a for a, b in zip(first, second, strict=True))
    right = tuple(c - a for a, c in zip(first, third, strict=True))
    cross = (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )
    length = math.sqrt(sum(value * value for value in cross))
    return (
        (0.0, 0.0, 0.0)
        if length <= 1e-15
        else tuple(value / length for value in cross)  # type: ignore[return-value]
    )


def _glb(parts: tuple[MeshPart, ...]) -> bytes:
    binary = bytearray()
    buffer_views: list[dict[str, object]] = []
    accessors: list[dict[str, object]] = []
    meshes: list[dict[str, object]] = []
    nodes: list[dict[str, object]] = []
    materials: list[dict[str, object]] = []
    material_by_role: dict[str, int] = {}
    for part in parts:
        if part.role not in material_by_role:
            material_by_role[part.role] = len(materials)
            materials.append(
                {
                    "name": part.role,
                    "pbrMetallicRoughness": {
                        "baseColorFactor": COLORS[part.role],
                        "metallicFactor": 0.1,
                        "roughnessFactor": 0.7,
                    },
                }
            )
        _pad4(binary)
        vertex_offset = len(binary)
        for vertex in part.mesh.vertices:
            binary.extend(struct.pack("<3f", *vertex))
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": vertex_offset,
                "byteLength": len(part.mesh.vertices) * 12,
                "target": 34962,
            }
        )
        vertex_view = len(buffer_views) - 1
        minimum = [min(vertex[index] for vertex in part.mesh.vertices) for index in range(3)]
        maximum = [max(vertex[index] for vertex in part.mesh.vertices) for index in range(3)]
        accessors.append(
            {
                "bufferView": vertex_view,
                "componentType": 5126,
                "count": len(part.mesh.vertices),
                "type": "VEC3",
                "min": minimum,
                "max": maximum,
            }
        )
        position_accessor = len(accessors) - 1
        _pad4(binary)
        index_offset = len(binary)
        indices = [index for face in part.mesh.faces for index in face]
        binary.extend(struct.pack(f"<{len(indices)}I", *indices))
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": index_offset,
                "byteLength": len(indices) * 4,
                "target": 34963,
            }
        )
        accessors.append(
            {
                "bufferView": len(buffer_views) - 1,
                "componentType": 5125,
                "count": len(indices),
                "type": "SCALAR",
                "min": [min(indices)],
                "max": [max(indices)],
            }
        )
        meshes.append(
            {
                "name": part.name,
                "primitives": [
                    {
                        "attributes": {"POSITION": position_accessor},
                        "indices": len(accessors) - 1,
                        "material": material_by_role[part.role],
                    }
                ],
            }
        )
        nodes.append(
            {
                "name": part.name,
                "mesh": len(meshes) - 1,
                "extras": {"role": part.role, "purpose": part.purpose},
            }
        )
    document = {
        "asset": {"version": "2.0", "generator": "fibsem_liftout_v1"},
        "scene": 0,
        "scenes": [{"name": "trusted-checkpoint", "nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    json_chunk = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    json_chunk += b" " * ((4 - len(json_chunk) % 4) % 4)
    _pad4(binary)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    return (
        struct.pack("<III", 0x46546C67, 2, total)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(binary), 0x004E4942)
        + bytes(binary)
    )


def _pad4(value: bytearray) -> None:
    value.extend(b"\0" * ((4 - len(value) % 4) % 4))


def _png(width: int, height: int, pixels: bytes) -> bytes:
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width < 1
        or height < 1
        or width > 8192
        or height > 8192
        or not isinstance(pixels, bytes)
        or len(pixels) != width * height
    ):
        raise ValueError("checkpoint image dimensions or bytes are invalid")
    raw = b"".join(
        b"\0" + pixels[row * width : (row + 1) * width] for row in range(height)
    )
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    ) + _png_chunk(b"IDAT", zlib.compress(raw, level=9)) + _png_chunk(b"IEND", b"")


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _digest(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} hash is invalid")


def _write_fsync(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
