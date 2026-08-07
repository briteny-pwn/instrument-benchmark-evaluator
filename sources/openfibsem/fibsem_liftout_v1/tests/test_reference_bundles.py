from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from sources.openfibsem.fibsem_liftout_v1.geometry.metrics import (
    Bounds,
    TriangleMesh,
    box_mesh,
)
from sources.openfibsem.fibsem_liftout_v1.models import ScenarioSpec
from sources.openfibsem.fibsem_liftout_v1.reference_bundles import (
    ReferenceBundleError,
    build_reference_bundle,
    derive_roi_set,
    load_reference_bundle,
)


ROOT = Path(__file__).resolve().parents[4]
NOMINAL = (
    ROOT.parent
    / "instance"
    / "sources"
    / "openfibsem"
    / "fibsem_liftout_v1"
    / "scenarios"
    / "nominal.json"
)


def combined_mesh(*meshes: TriangleMesh) -> TriangleMesh:
    vertices = []
    faces = []
    for mesh in meshes:
        offset = len(vertices)
        vertices.extend(mesh.vertices)
        faces.extend(
            tuple(index + offset for index in face) for face in mesh.faces
        )
    return TriangleMesh(tuple(vertices), tuple(faces))


def write_stl(path: Path, mesh: TriangleMesh) -> None:
    value = bytearray(b"private-reference-test".ljust(80, b"\0"))
    value.extend(struct.pack("<I", len(mesh.faces)))
    for triangle in mesh.triangles:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def build_fixture_bundle(root: Path, spec: ScenarioSpec) -> Path:
    sample_center = spec.world_position("sample")
    baseline = box_mesh(center=sample_center, size=spec.sample_dimensions_um)
    step_1 = box_mesh(center=sample_center, size=(12.0, 8.0, 10.0))
    step_2 = box_mesh(center=(-1.0, 0.0, 5.0), size=(12.0, 8.0, 10.0))
    step_3 = box_mesh(center=(-989.0, 0.0, 6.0), size=(12.0, 8.0, 10.0))
    step_4 = box_mesh(center=(-989.0, 0.0, 6.0), size=(11.5, 8.0, 10.0))
    protection = box_mesh(center=(0.0, 0.0, 10.25), size=(4.0, 2.0, 0.5))
    needle_joint = box_mesh(center=(-7.0, 0.0, 5.0), size=(2.0, 2.0, 2.0))
    target_joint = box_mesh(center=(-996.0, 0.0, 6.0), size=(2.0, 2.0, 2.0))

    write_stl(root / "baseline" / "sample.stl", baseline)
    samples = (step_1, step_2, step_3, step_4)
    depositions = (
        protection,
        combined_mesh(protection, needle_joint),
        combined_mesh(protection, needle_joint, target_joint),
        combined_mesh(protection, needle_joint, target_joint),
    )
    for index, (sample, deposition) in enumerate(
        zip(samples, depositions, strict=True), start=1
    ):
        step = root / f"step_{index}"
        write_stl(step / "sample.stl", sample)
        write_stl(step / "deposition.stl", deposition)

    build_reference_bundle(
        root,
        spec,
        openfibsem_commit="1" * 40,
        evaluator_commit="2" * 40,
        generator_tree_sha256="3" * 64,
        reference_solution_sha256="4" * 64,
    )
    return root


def test_reference_manifest_binds_scenario_algorithm_and_files(tmp_path: Path) -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    root = build_fixture_bundle(tmp_path / "nominal", spec)

    loaded = load_reference_bundle(root, spec)

    assert loaded.identity.algorithm_version == "stl-shape-v1"
    assert loaded.identity.scenario_id == "nominal"
    assert loaded.identity.scenario_sha256 == hashlib.sha256(
        spec.canonical_bytes()
    ).hexdigest()
    assert len(loaded.identity.file_sha256) == 9
    assert set(loaded.steps) == {"step_1", "step_2", "step_3", "step_4"}
    assert loaded.baseline_sample.evidence.watertight
    assert loaded.steps["step_3"].sample.evidence.volume_um3 > 0
    assert loaded.steps["step_3"].deposition.evidence.connected_component_count == 3


def test_reference_file_tamper_is_infrastructure_error(tmp_path: Path) -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    root = build_fixture_bundle(tmp_path / "nominal", spec)
    (root / "step_2" / "sample.stl").write_bytes(b"tampered")

    with pytest.raises(ReferenceBundleError, match="digest"):
        load_reference_bundle(root, spec)


def test_reference_manifest_rejects_a_different_scenario(tmp_path: Path) -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    root = build_fixture_bundle(tmp_path / "nominal", spec)
    changed = spec.to_dict()
    changed["scenario_id"] = "not-nominal"

    with pytest.raises(ReferenceBundleError, match="scenario"):
        load_reference_bundle(root, ScenarioSpec.from_dict(changed))


def test_reference_delta_rois_are_clipped_to_step_envelopes(tmp_path: Path) -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    bundle = load_reference_bundle(
        build_fixture_bundle(tmp_path / "nominal", spec), spec
    )

    rois = derive_roi_set(bundle, spec)

    step_1_envelope = Bounds((-12.0, -9.0, -3.0), (12.0, 9.0, 13.0))
    step_3_envelope = Bounds((-1003.0, -10.0, -3.0), (-975.0, 10.0, 15.0))
    assert step_1_envelope.contains(rois.step_1_cut.bounds)
    assert step_3_envelope.contains(rois.step_3_target_deposition.bounds)
    assert rois.source_bridge.bounds == Bounds((6.5, -1.0, 0.0), (7.5, 1.0, 2.0))
    assert rois.target_joint.bounds == Bounds(
        (-998.0, -3.0, 3.0), (-994.0, 3.0, 9.0)
    )


def test_reference_loader_rejects_symlinked_mesh(tmp_path: Path) -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    root = build_fixture_bundle(tmp_path / "nominal", spec)
    target = root / "step_1" / "sample.stl"
    outside = tmp_path / "outside.stl"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(ReferenceBundleError, match="regular file"):
        load_reference_bundle(root, spec)
