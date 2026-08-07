from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sources.openfibsem.fibsem_liftout_v1.artifact_scoring import (
    score_checkpoint_artifacts,
)
from sources.openfibsem.fibsem_liftout_v1.checkpoint_exporter import (
    CheckpointExporter,
    _png,
    _stl,
)
from sources.openfibsem.fibsem_liftout_v1.geometry.metrics import MeshPart, box_mesh
from sources.openfibsem.fibsem_liftout_v1.tests.fakes import valid_snapshot


def exported_bundle(tmp_path: Path) -> tuple[Path, object]:
    snapshot = valid_snapshot("step_1")
    exporter = CheckpointExporter(tmp_path)
    exporter.export(
        snapshot,
        {
            "SEM": (2, 2, bytes((0, 85, 170, 255))),
            "FIB": (2, 2, bytes((0, 85, 170, 255))),
        },
        world_id="nominal",
        journal_sequence=1,
        journal_hash="a" * 64,
    )
    return tmp_path / "artifacts" / "nominal" / "step_1", snapshot


def replace_indexed_file(root: Path, relative: str, payload: bytes) -> None:
    (root / relative).write_bytes(payload)
    checkpoint_path = root / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["artifacts"][relative] = {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    checkpoint_path.write_text(
        json.dumps(
            checkpoint,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n",
        encoding="ascii",
    )


def test_valid_checkpoint_bundle_receives_full_artifact_score(tmp_path: Path) -> None:
    root, snapshot = exported_bundle(tmp_path)

    result = score_checkpoint_artifacts(
        root,
        "nominal",
        "step_1",
        trusted_snapshot=snapshot,
        expected_resolution=(2, 2),
    )

    assert result.points == 2.5
    assert result.maximum_points == 2.5
    assert result.criteria["stl_topology"].points == 0.75
    assert result.criteria["scene_component_consistency"].points == 0.5
    assert result.criteria["glb_consistency"].points == 0.5
    assert result.criteria["sem_resolution"].points == 0.125
    assert result.criteria["sem_contrast"].points == 0.125
    assert result.criteria["sem_useful_tone"].points == 0.125
    assert result.criteria["fib_resolution"].points == 0.125
    assert result.criteria["fib_contrast"].points == 0.125
    assert result.criteria["fib_useful_tone"].points == 0.125
    assert result.components["sample.stl"].watertight


def test_scene_component_mismatch_loses_consistency_points(tmp_path: Path) -> None:
    root, _snapshot = exported_bundle(tmp_path)
    shifted = MeshPart(
        "shifted",
        "sample",
        box_mesh(center=(100.0, 0.0, 0.0), size=(1.0, 1.0, 1.0)),
    )
    replace_indexed_file(root, "scene.stl", _stl((shifted,)))

    result = score_checkpoint_artifacts(
        root,
        "nominal",
        "step_1",
        expected_resolution=(2, 2),
    )

    assert result.criteria["scene_component_consistency"].points == 0.0
    assert result.points == pytest.approx(2.0)


def test_constant_images_keep_format_points_but_lose_quality_points(
    tmp_path: Path,
) -> None:
    root, snapshot = exported_bundle(tmp_path)
    constant = _png(2, 2, bytes((128, 128, 128, 128)))
    replace_indexed_file(root, "sem.png", constant)
    replace_indexed_file(root, "fib.png", constant)

    result = score_checkpoint_artifacts(
        root,
        "nominal",
        "step_1",
        trusted_snapshot=snapshot,
        expected_resolution=(2, 2),
    )

    assert result.criteria["sem_resolution"].points == 0.125
    assert result.criteria["sem_contrast"].points == 0.0
    assert result.criteria["sem_useful_tone"].points == 0.0
    assert result.criteria["fib_resolution"].points == 0.125
    assert result.criteria["fib_contrast"].points == 0.0
    assert result.criteria["fib_useful_tone"].points == 0.0
    assert result.points == pytest.approx(2.0)


def test_wrong_image_resolution_loses_only_resolution_subcriterion(
    tmp_path: Path,
) -> None:
    root, snapshot = exported_bundle(tmp_path)

    result = score_checkpoint_artifacts(
        root,
        "nominal",
        "step_1",
        trusted_snapshot=snapshot,
        expected_resolution=(512, 512),
    )

    assert result.criteria["sem_resolution"].points == 0.0
    assert result.criteria["fib_resolution"].points == 0.0
    assert result.points == pytest.approx(2.25)
