from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from evaluators.fibsem_liftout_v1.backend import OpenFibsemBackend
from evaluators.fibsem_liftout_v1.checkpoint_exporter import CheckpointExporter
from evaluators.fibsem_liftout_v1.instrumented_microscope import OperationDispatcher
from evaluators.fibsem_liftout_v1.journal import EventJournal
from evaluators.fibsem_liftout_v1.scenario import load_fixed_scenarios, seeded_scenarios
from evaluators.fibsem_liftout_v1.scoring import (
    CheckpointEvidence,
    RuntimeEvidence,
    TerminalEvidence,
    grade_world,
)
from evaluators.fibsem_liftout_v1.service import FibsemService
from evaluators.fibsem_liftout_v1.tests.fakes import RecordingRuntime


ROOT = Path(__file__).resolve().parents[3]
INSTANCE = ROOT.parent / "instance" / "fibsem_liftout_v1"
NOMINAL = INSTANCE / "scenarios" / "nominal.json"
REFERENCE = Path(__file__).resolve().parents[1] / "reference" / "solution.py"


@pytest.fixture(scope="module", autouse=True)
def public_package_path():
    sys.path.insert(0, str(INSTANCE))
    try:
        yield
    finally:
        sys.path.remove(str(INSTANCE))


def reference_module():
    spec = importlib.util.spec_from_file_location("iab_reference_solution", REFERENCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def run_reference(spec, root: Path):
    from fibsem_iab import MicroscopeClient, Scenario
    from fibsem_iab.client import checkpoint_callback

    runtime = RecordingRuntime()
    backend = OpenFibsemBackend(spec, runtime=runtime)
    journal = EventJournal("reference-run", spec.scenario_id)
    service = FibsemService(backend, spec, journal, CheckpointExporter(root))
    dispatcher = OperationDispatcher(service, spec, journal)

    class DirectRpc:
        def call(self, operation, arguments=None):
            return dispatcher.dispatch(operation, dict(arguments or {}))

    microscope = MicroscopeClient(DirectRpc())
    scenario = Scenario.from_dict(spec.to_dict())
    result = reference_module().run_experiment(
        microscope,
        scenario,
        checkpoint_callback(microscope),
        root / "candidate-output",
    )
    summary = service.finalize(outcome="completed", forced=False)
    evidence = {
        step: CheckpointEvidence(
            step,
            frozen.geometry,
            True,
            frozen.artifacts.bundle_sha256,
        )
        for step, frozen in service.frozen_checkpoints.items()
    }
    pre = summary["cleanup"]["pre_cleanup"]
    terminal = TerminalEvidence(
        safe=not pre["inserted"] and pre["active_operation"] is None,
        simulator_idle=pre["active_operation"] is None,
        collision=bool(pre["collision"]),
        cleanup_error=summary["cleanup"]["error_type"],
    )
    runtime_evidence = RuntimeEvidence(0, False, False, False, 10001, 11001, True)
    return result, grade_world(spec, journal, evidence, terminal, runtime_evidence)


def test_reference_uses_only_public_imports() -> None:
    assert imported_roots(REFERENCE) <= {"fibsem_iab", "json", "math", "pathlib"}
    assert "evaluators" not in REFERENCE.read_text(encoding="utf-8")


def test_reference_passes_public_nominal_world(tmp_path: Path) -> None:
    nominal = load_fixed_scenarios(NOMINAL)["nominal"]

    result, report = run_reference(nominal, tmp_path / "nominal")

    assert result["completed"] is True
    assert report.score == 100
    assert report.strict_pass, report.to_dict()


def test_reference_passes_all_fixed_and_seeded_worlds(tmp_path: Path) -> None:
    worlds = list(load_fixed_scenarios(NOMINAL).values()) + list(
        seeded_scenarios(5, base_seed=47000, nominal_path=NOMINAL)
    )

    reports = [
        run_reference(world, tmp_path / world.scenario_id)[1] for world in worlds
    ]

    assert len(reports) == 10
    assert all(report.score == 100 and report.strict_pass for report in reports), [
        report.to_dict() for report in reports if not report.strict_pass
    ]
