from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from sources.openfibsem.fibsem_liftout_v1.backend import OpenFibsemBackend
from sources.openfibsem.fibsem_liftout_v1.checkpoint_exporter import CheckpointExporter
from sources.openfibsem.fibsem_liftout_v1.instrumented_microscope import OperationDispatcher
from sources.openfibsem.fibsem_liftout_v1.journal import EventJournal
from sources.openfibsem.fibsem_liftout_v1.scenario import load_fixed_scenarios
from sources.openfibsem.fibsem_liftout_v1.scoring import (
    CheckpointEvidence,
    RuntimeEvidence,
    TerminalEvidence,
    grade_world,
)
from sources.openfibsem.fibsem_liftout_v1.service import FibsemService
from sources.openfibsem.fibsem_liftout_v1.tests.fakes import RecordingRuntime


ROOT = Path(__file__).resolve().parents[4]
INSTANCE = ROOT.parent / "instance" / "sources" / "openfibsem" / "fibsem_liftout_v1"
NOMINAL = INSTANCE / "scenarios" / "nominal.json"
NEGATIVES = Path(__file__).resolve().parents[1] / "negatives"
MATRIX = {
    "cut_source_early": "necessary_partial_order",
    "no_source_cut": "all_checkpoint_states",
    "hardcoded_nominal": "all_checkpoint_states",
    "no_target_deposition": "all_checkpoint_states",
    "cut_needle_early": "necessary_partial_order",
    "cut_both_joints": "all_checkpoint_states",
    "no_retract": "safe_terminal_state",
    "fake_checkpoint": "trusted_artifacts_complete",
    "private_import": "no_forbidden_access",
}


@pytest.fixture(scope="module", autouse=True)
def candidate_import_paths():
    sys.path[:0] = [str(INSTANCE), str(NEGATIVES)]
    try:
        yield
    finally:
        sys.path.remove(str(INSTANCE))
        sys.path.remove(str(NEGATIVES))


def load_candidate(name: str):
    path = NEGATIVES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"iab_negative_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def run_negative(name: str, scenario, root: Path):
    from fibsem_iab import MicroscopeClient, Scenario
    from fibsem_iab.client import checkpoint_callback

    backend = OpenFibsemBackend(scenario, runtime=RecordingRuntime())
    journal = EventJournal(f"negative-{name}", scenario.scenario_id)
    service = FibsemService(backend, scenario, journal, CheckpointExporter(root))
    dispatcher = OperationDispatcher(service, scenario, journal)

    class DirectRpc:
        def call(self, operation, arguments=None):
            return dispatcher.dispatch(operation, dict(arguments or {}))

    exit_code = 0
    forbidden = name == "private_import"
    if forbidden:
        exit_code = 1
    else:
        microscope = MicroscopeClient(DirectRpc())
        try:
            load_candidate(name).run_experiment(
                microscope,
                Scenario.from_dict(scenario.to_dict()),
                checkpoint_callback(microscope),
                root / "candidate-output",
            )
        except Exception:
            exit_code = 1
    summary = service.finalize(
        outcome="completed" if exit_code == 0 else "candidate_failure",
        forced=exit_code != 0,
    )
    checkpoints = {
        step: CheckpointEvidence(
            step, frozen.geometry, True, frozen.artifacts.bundle_sha256
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
    runtime = RuntimeEvidence(
        exit_code, False, forbidden, False, 10001, 11001, True
    )
    return grade_world(scenario, journal, checkpoints, terminal, runtime)


def test_negative_helpers_do_not_import_trusted_evaluator_code() -> None:
    assert imported_roots(NEGATIVES / "workflow.py") == {"fibsem_iab"}
    for name in MATRIX:
        roots = imported_roots(NEGATIVES / f"{name}.py")
        if name == "private_import":
            assert "sources" in roots
        else:
            assert roots == {"workflow"}


@pytest.mark.parametrize(("name", "gate"), MATRIX.items())
def test_negative_fails_intended_gate(
    name: str, gate: str, tmp_path: Path
) -> None:
    worlds = load_fixed_scenarios(NOMINAL)
    scenario = worlds["target_pose"] if name == "hardcoded_nominal" else worlds["nominal"]

    report = run_negative(name, scenario, tmp_path / name)

    assert not report.strict_pass
    assert not report.strict_gates[gate], report.to_dict()
