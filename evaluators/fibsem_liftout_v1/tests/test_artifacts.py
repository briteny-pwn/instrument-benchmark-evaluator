from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path

import pytest

from evaluators.fibsem_liftout_v1.geometry.artifacts import (
    ArtifactError,
    REQUIRED_COMPONENTS,
    validate_checkpoint_bundle,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def binary_stl() -> bytes:
    header = b"iab-test".ljust(80, b"\0")
    triangle = struct.pack(
        "<12fH",
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0,
    )
    return header + struct.pack("<I", 1) + triangle


def minimal_glb() -> bytes:
    document = json.dumps({"asset": {"version": "2.0"}}, separators=(",", ":")).encode()
    document += b" " * ((4 - len(document) % 4) % 4)
    chunk = struct.pack("<II", len(document), 0x4E4F534A) + document
    return struct.pack("<III", 0x46546C67, 2, 12 + len(chunk)) + chunk


def write_valid_bundle(root: Path) -> None:
    components = root / "components"
    components.mkdir(parents=True, exist_ok=True)
    files = {
        "scene.glb": minimal_glb(),
        "scene.stl": binary_stl(),
        "sem.png": PNG_1X1,
        "fib.png": PNG_1X1,
    }
    for name in REQUIRED_COMPONENTS:
        files[f"components/{name}"] = binary_stl()
    for relative, payload in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    checkpoint = {
        "schema_version": 1,
        "world_id": "nominal",
        "step_id": "step_1",
        "journal_sequence": 10,
        "journal_hash": "a" * 64,
        "geometry_hash": "b" * 64,
        "artifacts": {
            relative: {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for relative, payload in sorted(files.items())
        },
    }
    (root / "checkpoint.json").write_text(
        json.dumps(checkpoint, sort_keys=True), encoding="utf-8"
    )


def test_valid_bundle_parses_and_verifies_every_digest(tmp_path: Path) -> None:
    write_valid_bundle(tmp_path)

    evidence = validate_checkpoint_bundle(tmp_path, expected_world="nominal", expected_step="step_1")

    assert evidence.world_id == "nominal"
    assert evidence.step_id == "step_1"
    assert len(evidence.bundle_sha256) == 64
    assert evidence.files["scene.glb"].bytes > 20


def test_bundle_rejects_digest_mismatch_and_unexpected_files(tmp_path: Path) -> None:
    write_valid_bundle(tmp_path)
    (tmp_path / "scene.stl").write_bytes(binary_stl() + b"tampered")
    with pytest.raises(ArtifactError, match="digest|size"):
        validate_checkpoint_bundle(tmp_path, expected_world="nominal", expected_step="step_1")

    write_valid_bundle(tmp_path)
    (tmp_path / "candidate-claim.txt").write_text("trusted")
    with pytest.raises(ArtifactError, match="unexpected"):
        validate_checkpoint_bundle(tmp_path, expected_world="nominal", expected_step="step_1")


def test_bundle_rejects_links_and_malformed_media(tmp_path: Path) -> None:
    write_valid_bundle(tmp_path)
    link = tmp_path / "components" / "sample.stl"
    link.unlink()
    link.symlink_to(tmp_path / "scene.stl")
    with pytest.raises(ArtifactError, match="link"):
        validate_checkpoint_bundle(tmp_path, expected_world="nominal", expected_step="step_1")

    link.unlink()
    link.write_bytes(b"not an STL")
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    payload = link.read_bytes()
    checkpoint["artifacts"]["components/sample.stl"] = {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    (tmp_path / "checkpoint.json").write_text(json.dumps(checkpoint))
    with pytest.raises(ArtifactError, match="STL"):
        validate_checkpoint_bundle(tmp_path, expected_world="nominal", expected_step="step_1")
