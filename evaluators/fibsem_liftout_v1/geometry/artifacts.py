from __future__ import annotations

import hashlib
import json
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .metrics import Bounds, SceneSnapshot
from .oracle import _canonical_geometry_hash


REQUIRED_COMPONENTS = (
    "source.stl",
    "sample.stl",
    "needle.stl",
    "target.stl",
    "deposition.stl",
)
REQUIRED_MEDIA = ("scene.glb", "scene.stl", "sem.png", "fib.png")
MAX_FILE_BYTES = 64 * 1024 * 1024


class ArtifactError(ValueError):
    """A trusted checkpoint artifact bundle is incomplete or inconsistent."""


@dataclass(frozen=True)
class FileEvidence:
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ArtifactEvidence:
    world_id: str
    step_id: str
    journal_sequence: int
    journal_hash: str
    geometry_hash: str
    files: Mapping[str, FileEvidence]
    bundle_sha256: str


def validate_checkpoint_bundle(
    root: Path,
    *,
    expected_world: str,
    expected_step: str,
    trusted_snapshot: SceneSnapshot | None = None,
) -> ArtifactEvidence:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ArtifactError("artifact root is not a directory")
    expected = {
        *REQUIRED_MEDIA,
        "checkpoint.json",
        *(f"components/{name}" for name in REQUIRED_COMPONENTS),
    }
    actual: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_dir() and not path.is_symlink():
            if relative != "components":
                raise ArtifactError(f"unexpected artifact directory: {relative}")
            continue
        actual.add(relative)
        _require_regular_file(path, relative, root)
    unexpected = actual - expected
    missing = expected - actual
    if unexpected:
        raise ArtifactError("unexpected artifact files: " + ", ".join(sorted(unexpected)))
    if missing:
        raise ArtifactError("missing artifact files: " + ", ".join(sorted(missing)))

    checkpoint_path = root / "checkpoint.json"
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"invalid checkpoint JSON: {exc}") from exc
    if not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != 1:
        raise ArtifactError("checkpoint schema version is invalid")
    if checkpoint.get("world_id") != expected_world or checkpoint.get("step_id") != expected_step:
        raise ArtifactError("checkpoint identity mismatch")
    sequence = checkpoint.get("journal_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ArtifactError("checkpoint journal sequence is invalid")
    journal_hash = _digest_field(checkpoint.get("journal_hash"), "journal")
    geometry_hash = _digest_field(checkpoint.get("geometry_hash"), "geometry")
    if (
        trusted_snapshot is not None
        and geometry_hash != _canonical_geometry_hash(trusted_snapshot.parts)
    ):
        raise ArtifactError("checkpoint geometry hash does not match trusted snapshot")
    declared = checkpoint.get("artifacts")
    expected_payloads = expected - {"checkpoint.json"}
    if not isinstance(declared, dict) or set(declared) != expected_payloads:
        raise ArtifactError("checkpoint artifact index is incomplete")

    files: dict[str, FileEvidence] = {}
    for relative in sorted(expected_payloads):
        path = root / relative
        payload = path.read_bytes()
        record = declared[relative]
        if not isinstance(record, dict) or set(record) != {"bytes", "sha256"}:
            raise ArtifactError(f"artifact record is invalid: {relative}")
        digest = hashlib.sha256(payload).hexdigest()
        if record["bytes"] != len(payload):
            raise ArtifactError(f"artifact size mismatch: {relative}")
        if record["sha256"] != digest:
            raise ArtifactError(f"artifact digest mismatch: {relative}")
        if relative.endswith(".stl"):
            _validate_stl(payload, relative)
        elif relative.endswith(".png"):
            _validate_png(payload, relative)
        elif relative.endswith(".glb"):
            _validate_glb(payload, relative)
        files[relative] = FileEvidence(len(payload), digest)
    if trusted_snapshot is not None:
        _validate_snapshot_meshes(root, trusted_snapshot)
    checkpoint_payload = checkpoint_path.read_bytes()
    bundle_index = {
        **{name: evidence.sha256 for name, evidence in sorted(files.items())},
        "checkpoint.json": hashlib.sha256(checkpoint_payload).hexdigest(),
    }
    bundle_hash = hashlib.sha256(
        json.dumps(bundle_index, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ArtifactEvidence(
        expected_world,
        expected_step,
        sequence,
        journal_hash,
        geometry_hash,
        files,
        bundle_hash,
    )


def _require_regular_file(path: Path, relative: str, root: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ArtifactError(f"artifact link or non-regular file: {relative}")
    if info.st_nlink != 1:
        raise ArtifactError(f"artifact hard link is forbidden: {relative}")
    if info.st_size <= 0 or info.st_size > MAX_FILE_BYTES:
        raise ArtifactError(f"artifact size is invalid: {relative}")
    if not path.resolve().is_relative_to(root):
        raise ArtifactError(f"artifact path escapes root: {relative}")


def _digest_field(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ArtifactError(f"checkpoint {name} hash is invalid")
    return value


def _validate_stl(payload: bytes, relative: str) -> None:
    if len(payload) >= 84:
        triangles = struct.unpack("<I", payload[80:84])[0]
        if triangles > 0 and len(payload) == 84 + 50 * triangles:
            return
    text = payload[:1024].lstrip().lower()
    if text.startswith(b"solid") and b"facet" in payload.lower() and b"endsolid" in payload.lower():
        return
    raise ArtifactError(f"invalid STL: {relative}")


def _validate_png(payload: bytes, relative: str) -> None:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise ArtifactError(f"invalid PNG: {relative}")
    width, height = struct.unpack(">II", payload[16:24])
    if width < 1 or height < 1 or width > 8192 or height > 8192:
        raise ArtifactError(f"invalid PNG dimensions: {relative}")


def _validate_glb(payload: bytes, relative: str) -> None:
    if len(payload) < 20:
        raise ArtifactError(f"invalid GLB: {relative}")
    magic, version, length = struct.unpack("<III", payload[:12])
    if magic != 0x46546C67 or version != 2 or length != len(payload):
        raise ArtifactError(f"invalid GLB header: {relative}")
    chunk_length, chunk_type = struct.unpack("<II", payload[12:20])
    if chunk_type != 0x4E4F534A or 20 + chunk_length > len(payload):
        raise ArtifactError(f"invalid GLB JSON chunk: {relative}")
    try:
        document = json.loads(payload[20 : 20 + chunk_length].rstrip(b" \0"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"invalid GLB JSON: {relative}") from exc
    if not isinstance(document, dict) or document.get("asset", {}).get("version") != "2.0":
        raise ArtifactError(f"invalid GLB asset: {relative}")


def _validate_snapshot_meshes(root: Path, snapshot: SceneSnapshot) -> None:
    expected_roles = {
        "source.stl": {"source"},
        "sample.stl": {"sample"},
        "needle.stl": {"needle"},
        "target.stl": {"target"},
        "deposition.stl": {"deposition"},
    }
    expected = {
        "scene.stl": _parts_bounds(snapshot.parts),
        **{
            f"components/{filename}": _parts_bounds(
                tuple(part for part in snapshot.parts if part.role in roles)
            )
            for filename, roles in expected_roles.items()
        },
    }
    for relative, bounds in expected.items():
        if bounds is None:
            raise ArtifactError(f"trusted snapshot is missing component: {relative}")
        actual = _binary_stl_bounds((root / relative).read_bytes(), relative)
        if any(
            abs(expected_value - actual_value) > 1e-5
            for expected_value, actual_value in zip(
                (*bounds.minimum, *bounds.maximum),
                (*actual.minimum, *actual.maximum),
                strict=True,
            )
        ):
            raise ArtifactError(f"artifact bounds disagree with trusted snapshot: {relative}")


def _parts_bounds(parts: tuple[object, ...]) -> Bounds | None:
    meshes = [getattr(part, "mesh", None) for part in parts]
    bounds = [mesh.bounds for mesh in meshes if mesh is not None]
    if not bounds:
        return None
    return Bounds(
        tuple(min(item.minimum[index] for item in bounds) for index in range(3)),  # type: ignore[arg-type]
        tuple(max(item.maximum[index] for item in bounds) for index in range(3)),  # type: ignore[arg-type]
    )


def _binary_stl_bounds(payload: bytes, relative: str) -> Bounds:
    if len(payload) < 84:
        raise ArtifactError(f"trusted comparison requires binary STL: {relative}")
    count = struct.unpack("<I", payload[80:84])[0]
    if count < 1 or len(payload) != 84 + 50 * count:
        raise ArtifactError(f"trusted comparison requires binary STL: {relative}")
    vertices: list[tuple[float, float, float]] = []
    for index in range(count):
        values = struct.unpack("<12fH", payload[84 + index * 50 : 134 + index * 50])
        vertices.extend((values[3:6], values[6:9], values[9:12]))
    return Bounds(
        tuple(min(vertex[index] for vertex in vertices) for index in range(3)),  # type: ignore[arg-type]
        tuple(max(vertex[index] for vertex in vertices) for index in range(3)),  # type: ignore[arg-type]
    )
