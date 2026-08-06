from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from sources.openfibsem.fibsem_liftout_v1.checkpoint_exporter import CheckpointExporter
from sources.openfibsem.fibsem_liftout_v1.geometry.artifacts import REQUIRED_COMPONENTS
from sources.openfibsem.fibsem_liftout_v1.geometry.artifacts import (
    ArtifactError,
    validate_checkpoint_bundle,
)
from sources.openfibsem.fibsem_liftout_v1.tests.fakes import valid_snapshot


def test_exporter_writes_merged_and_component_meshes_glb_images_and_manifest(
    tmp_path: Path,
) -> None:
    exporter = CheckpointExporter(tmp_path)
    image = (2, 2, bytes((0, 85, 170, 255)))

    evidence = exporter.export(
        valid_snapshot(),
        {"SEM": image, "FIB": image},
        world_id="nominal",
        journal_sequence=10,
        journal_hash="a" * 64,
    )

    root = tmp_path / "artifacts" / "nominal" / "step_1"
    assert (root / "scene.glb").read_bytes()[:4] == b"glTF"
    assert (root / "scene.stl").is_file()
    assert {path.name for path in (root / "components").glob("*.stl")} == set(
        REQUIRED_COMPONENTS
    )
    assert evidence.bundle_sha256
    assert stat.S_IMODE(root.stat().st_mode) == 0o777
    assert stat.S_IMODE((root / "components").stat().st_mode) == 0o777
    assert stat.S_IMODE(root.parent.stat().st_mode) == 0o777


def test_exporter_is_atomic_and_leaves_no_step_on_invalid_image(tmp_path: Path) -> None:
    exporter = CheckpointExporter(tmp_path)

    with pytest.raises(ValueError, match="image"):
        exporter.export(
            valid_snapshot(),
            {"SEM": (2, 2, b"short"), "FIB": (2, 2, bytes(4))},
            world_id="nominal",
            journal_sequence=10,
            journal_hash="a" * 64,
        )

    assert not (tmp_path / "artifacts" / "nominal" / "step_1").exists()
    assert not list(tmp_path.rglob("*.tmp"))


def test_exporter_publishes_readable_leaves_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    exporter = CheckpointExporter(tmp_path)
    image = (2, 2, bytes((0, 85, 170, 255)))
    previous_umask = os.umask(0o077)
    try:
        exporter.export(
            valid_snapshot(),
            {"SEM": image, "FIB": image},
            world_id="nominal",
            journal_sequence=10,
            journal_hash="a" * 64,
        )
    finally:
        os.umask(previous_umask)

    root = tmp_path / "artifacts" / "nominal" / "step_1"
    leaves = [path for path in root.rglob("*") if path.is_file()]
    assert leaves
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in leaves)


def test_exporter_sets_publishable_directory_mode_before_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sources.openfibsem.fibsem_liftout_v1.checkpoint_exporter as exporter_module

    observed_modes: list[int] = []
    real_replace = os.replace

    def recording_replace(source: Path, destination: Path) -> None:
        observed_modes.append(stat.S_IMODE(Path(source).stat().st_mode))
        real_replace(source, destination)

    monkeypatch.setattr(exporter_module.os, "replace", recording_replace)
    image = (2, 2, bytes((0, 85, 170, 255)))
    CheckpointExporter(tmp_path).export(
        valid_snapshot(),
        {"SEM": image, "FIB": image},
        world_id="nominal",
        journal_sequence=10,
        journal_hash="a" * 64,
    )

    assert observed_modes == [0o777]


def test_trusted_snapshot_rejects_self_consistent_but_wrong_component_mesh(
    tmp_path: Path,
) -> None:
    snapshot = valid_snapshot()
    exporter = CheckpointExporter(tmp_path)
    image = (2, 2, bytes((0, 85, 170, 255)))
    exporter.export(
        snapshot,
        {"SEM": image, "FIB": image},
        world_id="nominal",
        journal_sequence=10,
        journal_hash="a" * 64,
    )
    root = tmp_path / "artifacts" / "nominal" / "step_1"
    wrong = (root / "components" / "source.stl").read_bytes()
    sample_path = root / "components" / "sample.stl"
    sample_path.write_bytes(wrong)
    checkpoint_path = root / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["artifacts"]["components/sample.stl"] = {
        "bytes": len(wrong),
        "sha256": hashlib.sha256(wrong).hexdigest(),
    }
    checkpoint_path.write_text(json.dumps(checkpoint))

    with pytest.raises(ArtifactError, match="bounds"):
        validate_checkpoint_bundle(
            root,
            expected_world="nominal",
            expected_step="step_1",
            trusted_snapshot=snapshot,
        )
