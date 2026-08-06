from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from evaluators.fibsem_liftout_v1.backend import OpenFibsemBackend
from evaluators.fibsem_liftout_v1.geometry.oracle import GeometryOracle
from evaluators.fibsem_liftout_v1.models import ScenarioSpec
from evaluators.fibsem_liftout_v1.tests.fakes import RecordingRuntime


ROOT = Path(__file__).resolve().parents[3]
NOMINAL = ROOT.parent / "instance" / "fibsem_liftout_v1" / "scenarios" / "nominal.json"
PINNED = "2ebccb8b9721234ca66bb94de36d0f7cfe047af9"


def test_backend_uses_pinned_runtime_and_freezes_independent_mesh_copies() -> None:
    runtime = RecordingRuntime()
    backend = OpenFibsemBackend(ScenarioSpec.from_path(NOMINAL), runtime=runtime)

    assert backend.invoke("ping", {})["source_commit"] == PINNED  # type: ignore[index]
    first = backend.freeze_snapshot("step_1")
    second = backend.freeze_snapshot("step_1")

    assert first is not second
    assert first.parts is not second.parts
    assert GeometryOracle(ScenarioSpec.from_path(NOMINAL)).evaluate(first).sample_to_source
    assert any(call[0] == "synchronize" for call in runtime.calls if isinstance(call, tuple))


def test_backend_applies_joint_source_release_transfer_and_final_separation() -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    backend = OpenFibsemBackend(spec, runtime=RecordingRuntime())
    backend.invoke("insert_manipulator", {"position_um": [-8.5, 0.0, 5.0]})
    backend.invoke(
        "run_deposition",
        {
            "pattern": {
                "purpose": "needle_joint",
                "frame": "sample",
                "center_um": [-7.0, 0.0, 0.0],
                "size_um": [3.0, 1.0, 1.0],
                "rotation_degrees": 0.0,
            }
        },
    )
    step_1 = GeometryOracle(spec).evaluate(backend.freeze_snapshot("step_1"))
    assert step_1.sample_to_source and step_1.sample_to_needle

    backend.invoke(
        "run_cut",
        {
            "pattern": {
                "purpose": "source_separation",
                "frame": "sample",
                "center_um": [6.0, 0.0, -5.0],
                "size_um": [2.0, 2.0, 2.0],
                "rotation_degrees": 0.0,
            }
        },
    )
    backend.invoke(
        "move_manipulator", {"position_um": [-1.0, 0.0, 0.0], "relative": True}
    )
    step_2 = GeometryOracle(spec).evaluate(backend.freeze_snapshot("step_2"))
    assert not step_2.sample_to_source and step_2.sample_to_needle


def test_force_safe_detaches_needle_without_moving_target_attached_sample() -> None:
    spec = ScenarioSpec.from_path(NOMINAL)
    backend = OpenFibsemBackend(spec, runtime=RecordingRuntime())
    backend.invoke("insert_manipulator", {"position_um": [-8.5, 0.0, 5.0]})
    backend.invoke(
        "run_deposition",
        {
            "pattern": {
                "purpose": "needle_joint",
                "frame": "sample",
                "center_um": [-7.0, 0.0, 0.0],
                "size_um": [3.0, 1.0, 1.0],
                "rotation_degrees": 0.0,
            }
        },
    )
    backend.invoke(
        "run_cut",
        {
            "pattern": {
                "purpose": "source_separation",
                "frame": "sample",
                "center_um": [7.0, 0.0, -4.0],
                "size_um": [2.0, 2.0, 3.0],
                "rotation_degrees": 0.0,
            }
        },
    )
    target = spec.world_position("target_pose")
    current = spec.world_position("sample")
    backend.invoke(
        "move_manipulator",
        {
            "position_um": [
                target[index] - current[index] for index in range(3)
            ],
            "relative": True,
        },
    )
    backend.invoke(
        "run_deposition",
        {
            "pattern": {
                "purpose": "target_joint",
                "frame": "target",
                "center_um": [-16.0, 0.0, 6.0],
                "size_um": [4.0, 4.0, 4.0],
                "rotation_degrees": 0.0,
            }
        },
    )
    before = backend.freeze_snapshot("step_3").poses["sample"].position_um

    backend.force_safe()
    after_snapshot = backend.freeze_snapshot("step_4")

    assert after_snapshot.poses["sample"].position_um == pytest.approx(before)
    metrics = GeometryOracle(spec).evaluate(after_snapshot)
    assert metrics.sample_to_target
    assert not metrics.sample_to_needle


@pytest.mark.openfibsem
@pytest.mark.skipif(importlib.util.find_spec("fibsem") is None, reason="OpenFIBSEM not installed")
def test_real_openfibsem_runtime_import_reports_pinned_source() -> None:
    from evaluators.fibsem_liftout_v1.backend import openfibsem_source_commit

    assert openfibsem_source_commit() == PINNED
