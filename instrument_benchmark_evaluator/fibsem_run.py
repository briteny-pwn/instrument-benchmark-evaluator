from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from evaluators.fibsem_liftout_v1.models import ScenarioSpec
from evaluators.fibsem_liftout_v1.scenario import (
    load_fixed_scenarios,
    seeded_scenarios,
)
from evaluators.fibsem_liftout_v1.scenario import canonical_document
from evaluators.fibsem_liftout_v1.scoring import (
    FibsemEvaluationReport,
    FibsemWorldReport,
    RuntimeEvidence,
    TerminalEvidence,
    aggregate_worlds,
    grade_world,
)

from .candidate_backend import CandidateBackend, CandidateProcessResult
from .container.fibsem_sim_runner import (
    FibsemSimContainerResult,
    FibsemSimContainerRunner,
)
from .container.runner import ContainerProcessResult
from .contracts import InstanceSettings, RunSettings
from .isolation import IsolationError, prepare_workspace


FIXED_ORDER = ("nominal", "small", "large", "needle_offset", "target_pose")


@dataclass(frozen=True)
class FibsemWorldExecution:
    process: CandidateProcessResult | None
    sim_result: FibsemSimContainerResult | None
    report: FibsemWorldReport
    evidence_root: Path | None


def fibsem_suite_specs(
    instance_path: Path, *, repeated_base_seed: int
) -> tuple[ScenarioSpec, ...]:
    nominal = Path(instance_path).resolve() / "scenarios" / "nominal.json"
    fixed = load_fixed_scenarios(nominal)
    if tuple(fixed) != FIXED_ORDER:
        raise ValueError("FIBSEM fixed scenario order is invalid")
    repeated = seeded_scenarios(
        5,
        base_seed=repeated_base_seed,
        nominal_path=nominal,
    )
    return tuple(fixed[name] for name in FIXED_ORDER) + repeated


def run_fibsem_world(
    *,
    benchmark: RunSettings,
    instance: InstanceSettings,
    spec: ScenarioSpec,
    candidate_path: Path,
    backend: CandidateBackend,
    sim_runner: FibsemSimContainerRunner,
) -> FibsemWorldExecution:
    process: CandidateProcessResult | None = None
    sim_result: FibsemSimContainerResult | None = None
    root = Path(
        tempfile.mkdtemp(prefix="fibsem-w-", dir=benchmark.shared_run_root)
    ).resolve()
    root.chmod(0o755)
    transport = root / "transport"
    evidence = root / "evidence"
    workspace = root / "workspace"
    output = root / "output"
    for path in (transport, evidence, workspace, output):
        path.mkdir(mode=0o755)
    output.chmod(0o777)
    try:
        prepare_workspace(
            benchmark.instance_path,
            candidate_path,
            instance,
            workspace,
        )
    except IsolationError:
        return _candidate_boundary_failure(spec, evidence)
    scenario_payload = canonical_document(spec.to_dict())
    world_path = root / "world.json"
    candidate_scenario = workspace / "scenario.json"
    world_path.write_bytes(scenario_payload)
    candidate_scenario.write_bytes(scenario_payload)
    world_path.chmod(0o444)
    candidate_scenario.chmod(0o444)
    try:
        handle = sim_runner.start(
            run_id=benchmark.run_id,
            world_id=spec.scenario_id,
            world_path=world_path,
            transport_dir=transport,
            evidence_dir=evidence,
        )
    except Exception as exc:
        return _infrastructure_failure(spec, evidence, exc)
    failure: Exception | None = None
    try:
        try:
            process = backend.invoke(
                workspace=workspace,
                candidate_path=candidate_path,
                endpoint=handle.endpoint,
                instance=instance,
                timeout_seconds=benchmark.timeout_seconds,
                max_output_bytes=benchmark.max_output_bytes,
                run_id=benchmark.run_id,
                world_id=spec.scenario_id,
            )
        except Exception as exc:
            failure = exc
    finally:
        try:
            sim_result = sim_runner.finalize(handle)
        except Exception as exc:
            if failure is None:
                failure = exc
    if failure is not None or sim_result is None:
        return _infrastructure_failure(spec, evidence, failure or RuntimeError("sim failed"), process)
    trusted = sim_result.trusted_evidence
    infrastructure = trusted.outcome in {"infrastructure_failure", "cleanup_failure"}
    infrastructure = infrastructure or process.status == "infrastructure_failure"
    runtime = _runtime_evidence(
        process,
        sim_result,
        infrastructure=infrastructure,
        expected_world=spec.scenario_id,
    )
    report = grade_world(
        spec,
        trusted.journal,
        trusted.checkpoints,
        trusted.terminal,
        runtime,
    )
    return FibsemWorldExecution(process, sim_result, report, evidence)


def run_fibsem_full_suite(
    *,
    benchmark: RunSettings,
    instance: InstanceSettings,
    candidate_path: Path,
    backend: CandidateBackend,
    sim_runner: FibsemSimContainerRunner,
    repeated_base_seed: int,
) -> FibsemEvaluationReport:
    if benchmark.repeated_worlds != 5:
        raise ValueError("FIBSEM requires exactly five seeded worlds")
    if benchmark.fixed_worlds != FIXED_ORDER:
        raise ValueError("FIBSEM fixed world order is invalid")
    specs = fibsem_suite_specs(
        benchmark.instance_path,
        repeated_base_seed=repeated_base_seed,
    )
    reports = tuple(
        run_fibsem_world(
            benchmark=benchmark,
            instance=instance,
            spec=spec,
            candidate_path=candidate_path,
            backend=backend,
            sim_runner=sim_runner,
        ).report
        for spec in specs
    )
    return aggregate_worlds(reports)


def _runtime_evidence(
    process: CandidateProcessResult | None,
    sim_result: FibsemSimContainerResult | None,
    *,
    infrastructure: bool,
    forbidden: bool = False,
    expected_world: str | None = None,
) -> RuntimeEvidence:
    completed = process is not None and process.status == "completed"
    result_valid = completed and _candidate_result_valid(
        process.result, expected_world=expected_world
    )
    exit_code = (
        0
        if result_valid
        else process.returncode
        if process is not None and process.returncode not in {None, 0}
        else 3
        if process is not None
        else None
    )
    timed_out = process is not None and process.status == "candidate_timeout"
    text = "" if process is None else f"{process.stdout}\n{process.stderr}".lower()
    forbidden = forbidden or any(
        marker in text
        for marker in (
            "filesystem access denied",
            "network access denied",
            "process/native access denied",
            "forbidden import",
        )
    )
    candidate_container = (
        process.container_evidence
        if isinstance(process, ContainerProcessResult)
        else None
    )
    sim_container = sim_result.container_evidence if sim_result is not None else None
    isolation = (
        candidate_container is not None
        and sim_container is not None
        and candidate_container.user == "10001:10001"
        and sim_container.user == "11001:11001"
        and candidate_container.network_mode == "none"
        and sim_container.network_mode == "none"
        and candidate_container.readonly_rootfs
        and sim_container.readonly_rootfs
        and candidate_container.cleanup_succeeded
        and sim_container.cleanup_succeeded
    )
    return RuntimeEvidence(
        candidate_exit_code=exit_code,
        timed_out=timed_out,
        forbidden_access=forbidden,
        infrastructure_failure=infrastructure,
        candidate_uid=10001,
        simulator_uid=11001,
        isolation_verified=isolation,
    )


def _candidate_result_valid(value: object, *, expected_world: str | None) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value).issubset(
            {"instance_id", "scenario_id", "checkpoints", "completed", "notes"}
        )
        and {"instance_id", "scenario_id", "checkpoints", "completed"} <= set(value)
        and value["instance_id"] == "fibsem_liftout_v1"
        and isinstance(value["scenario_id"], str)
        and (expected_world is None or value["scenario_id"] == expected_world)
        and value["checkpoints"] == ["step_1", "step_2", "step_3", "step_4"]
        and value["completed"] is True
        and ("notes" not in value or isinstance(value["notes"], str))
    )


def _candidate_boundary_failure(
    spec: ScenarioSpec, evidence_root: Path
) -> FibsemWorldExecution:
    from evaluators.fibsem_liftout_v1.journal import EventJournal

    journal = EventJournal("candidate-boundary", spec.scenario_id)
    terminal = TerminalEvidence(True, True, False, None)
    runtime = RuntimeEvidence(None, False, True, False, 10001, 11001, True)
    report = grade_world(spec, journal, {}, terminal, runtime)
    return FibsemWorldExecution(None, None, report, evidence_root)


def _infrastructure_failure(
    spec: ScenarioSpec,
    evidence_root: Path,
    failure: Exception,
    process: CandidateProcessResult | None = None,
) -> FibsemWorldExecution:
    del failure
    from evaluators.fibsem_liftout_v1.journal import EventJournal

    journal = EventJournal("infrastructure", spec.scenario_id)
    terminal = TerminalEvidence(True, True, False, None)
    runtime = _runtime_evidence(process, None, infrastructure=True)
    report = grade_world(spec, journal, {}, terminal, runtime)
    return FibsemWorldExecution(process, None, report, evidence_root)
