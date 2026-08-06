from __future__ import annotations

import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from sources.pyvisa.pyvisa_dut_validation_v1.models import WorldSnapshot, WorldSpec
from sources.pyvisa.pyvisa_dut_validation_v1.scoring import (
    WorldReport,
    aggregate_reports,
    grade_run,
)
from sources.pyvisa.pyvisa_dut_validation_v1.worlds import repeated_specs
from sources.pyvisa.pyvisa_dut_validation_v2.journal import JournalEvent
from sources.pyvisa.pyvisa_dut_validation_v2.projection import project_events
from sources.pyvisa.pyvisa_dut_validation_v2.reports import (
    V2EvaluationReport,
    V2WorldReport,
)
from sources.pyvisa.pyvisa_dut_validation_v2.world_contract import dump_world

from .candidate_backend import CandidateBackend, CandidateProcessResult
from .container.runner import ContainerProcessResult
from .contracts import InstanceSettings, RunSettings
from .isolation import prepare_workspace
from .run import attach_runtime_evidence, dataclass_replace_status

if TYPE_CHECKING:
    from .container.sim_runner import SimContainerResult, SimContainerRunner


@dataclass(frozen=True)
class V2WorldExecution:
    process: CandidateProcessResult | None
    sim_result: SimContainerResult | None
    report: V2WorldReport


def run_v2_world(
    *,
    benchmark: RunSettings,
    instance: InstanceSettings,
    spec: WorldSpec,
    candidate_path: Path,
    backend: CandidateBackend,
    sim_runner: SimContainerRunner,
) -> V2WorldExecution:
    process: CandidateProcessResult | None = None
    sim_result: SimContainerResult | None = None
    failure: Exception | None = None
    with tempfile.TemporaryDirectory(
        prefix="v2-w-", dir=benchmark.shared_run_root
    ) as directory:
        root = Path(directory)
        root.chmod(0o755)
        transport = root / "transport"
        evidence = root / "evidence"
        workspace = root / "workspace"
        output = root / "output"
        for path in (transport, evidence, workspace, output):
            path.mkdir(mode=0o755)
        output.chmod(0o777)
        prepare_workspace(
            benchmark.instance_path, candidate_path, instance, workspace
        )
        world = root / "world.json"
        dump_world(spec, world)
        world.chmod(0o444)
        try:
            handle = sim_runner.start(
                run_id=benchmark.run_id,
                world_id=spec.world_id,
                world_path=world,
                transport_dir=transport,
                evidence_dir=evidence,
            )
        except Exception as exc:
            failure = exc
            return _infrastructure_execution(spec, None, None, failure)
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
                    world_id=spec.world_id,
                )
            except Exception as exc:
                failure = exc
        finally:
            try:
                sim_result = sim_runner.finalize(handle)
            except Exception as exc:
                if failure is None:
                    failure = exc
        if failure is not None or sim_result is None or sim_result.fatal is not None:
            return _infrastructure_execution(
                spec,
                process,
                sim_result,
                failure or RuntimeError("trusted simulator failed"),
            )
        try:
            raw = tuple(JournalEvent(**event) for event in sim_result.journal_evidence.events)
            projected = project_events(raw)
            pre_cleanup = _snapshot(
                sim_result.journal_evidence.pre_cleanup_snapshot
            )
            base = grade_run(
                process.result if process is not None else None,
                projected,
                spec,
                pre_cleanup,
                forbidden_access=any(
                    event.operation in {"protocol_reject", "rpc_reject"}
                    for event in projected
                ),
                infrastructure_ok=True,
            )
        except Exception as exc:
            return _infrastructure_execution(spec, process, sim_result, exc)
        assert process is not None
        if process.status != "completed":
            base = dataclass_replace_status(base, process.status)
        base = _attach_v2_runtime_evidence(
            base, process, forced_cleanup=not pre_cleanup.safe
        )
        report = V2WorldReport(
            base=base,
            candidate_container_evidence=base.container_evidence,
            sim_container_evidence=sim_result.container_evidence.to_dict(),
            sim_journal_evidence=sim_result.journal_evidence.to_dict(),
        )
        return V2WorldExecution(process, sim_result, report)


def run_v2_full_suite(
    *,
    benchmark: RunSettings,
    instance: InstanceSettings,
    specs: Mapping[str, WorldSpec],
    candidate_path: Path,
    backend: CandidateBackend,
    sim_runner: SimContainerRunner,
    repeated_base_seed: int,
) -> V2EvaluationReport:
    if benchmark.repeated_worlds != 10:
        raise ValueError("v2 requires exactly ten repeated worlds")
    missing = [name for name in benchmark.fixed_worlds if name not in specs]
    if missing:
        raise ValueError("missing fixed v2 worlds: " + ", ".join(missing))
    fixed = tuple(
        run_v2_world(
            benchmark=benchmark,
            instance=instance,
            spec=specs[name],
            candidate_path=candidate_path,
            backend=backend,
            sim_runner=sim_runner,
        )
        for name in benchmark.fixed_worlds
    )
    repeated = tuple(
        run_v2_world(
            benchmark=benchmark,
            instance=instance,
            spec=spec,
            candidate_path=candidate_path,
            backend=backend,
            sim_runner=sim_runner,
        )
        for spec in repeated_specs(10, base_seed=repeated_base_seed)
    )
    base = aggregate_reports(
        (execution.report.base for execution in fixed),
        (execution.report.base for execution in repeated),
    )
    return V2EvaluationReport(
        base, tuple(execution.report for execution in fixed + repeated)
    )


def _infrastructure_execution(
    spec: WorldSpec,
    process: CandidateProcessResult | None,
    sim_result: SimContainerResult | None,
    failure: Exception,
) -> V2WorldExecution:
    safe = WorldSnapshot(0, (), 0.0, False, None, (), 1.0, False, None, True)
    base = grade_run(None, (), spec, safe, infrastructure_ok=False)
    if process is not None:
        base = _attach_v2_runtime_evidence(
            base, process, forced_cleanup=False
        )
    base = replace(
        base,
        status="infrastructure_failure",
        strict_pass=False,
        infrastructure_valid=False,
        retry_eligible=True,
        errors=base.errors + (str(failure),),
    )
    report = V2WorldReport(
        base=base,
        candidate_container_evidence=base.container_evidence,
        sim_container_evidence=(
            sim_result.container_evidence.to_dict()
            if sim_result is not None
            else None
        ),
        sim_journal_evidence=(
            sim_result.journal_evidence.to_dict()
            if sim_result is not None
            else None
        ),
    )
    return V2WorldExecution(process, sim_result, report)


def _snapshot(value: dict | None) -> WorldSnapshot:
    if value is None:
        raise ValueError("sim pre-cleanup snapshot is missing")
    copied = dict(value)
    copied["closed_routes"] = tuple(copied["closed_routes"])
    copied["awg_points"] = tuple(copied["awg_points"])
    return WorldSnapshot(**copied)


def _attach_v2_runtime_evidence(
    report: WorldReport,
    process: CandidateProcessResult,
    *,
    forced_cleanup: bool,
) -> WorldReport:
    attached = attach_runtime_evidence(
        report, process, forced_cleanup=forced_cleanup
    )
    if not isinstance(process, ContainerProcessResult):
        return attached
    evidence = dict(attached.container_evidence or {})
    evidence["candidate_status"] = process.candidate_status or process.status
    return replace(attached, container_evidence=evidence)
