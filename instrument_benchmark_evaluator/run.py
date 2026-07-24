from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from evaluators.pyvisa_dut_validation_v1.gateway.journal import EventJournal
from evaluators.pyvisa_dut_validation_v1.gateway.server import GatewayServer
from evaluators.pyvisa_dut_validation_v1.instruments import InstrumentRack
from evaluators.pyvisa_dut_validation_v1.models import WorldSpec
from evaluators.pyvisa_dut_validation_v1.scoring import (
    EvaluationReport,
    WorldReport,
    aggregate_reports,
    grade_run,
)
from evaluators.pyvisa_dut_validation_v1.worlds import (
    load_world_specs,
    repeated_specs,
)

from .contracts import InstanceSettings, RunSettings
from .isolation import prepare_workspace
from .submission import ProcessResult, invoke_candidate


@dataclass(frozen=True)
class WorldExecution:
    process: ProcessResult
    report: WorldReport


def run_world(
    *,
    benchmark: RunSettings,
    instance: InstanceSettings,
    spec: WorldSpec,
    candidate_path: Path,
) -> WorldExecution:
    with tempfile.TemporaryDirectory(prefix="iab-experiment-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        prepare_workspace(
            benchmark.instance_path,
            candidate_path,
            instance,
            workspace,
        )
        rack = InstrumentRack(
            spec,
            resource_order="normal",
        )
        journal = EventJournal(f"{spec.world_id}-{spec.seed}", spec.world_id)
        endpoint = root / "gateway.sock"
        server = GatewayServer(endpoint, rack, journal=journal)
        server.start()
        try:
            process = invoke_candidate(
                workspace,
                endpoint,
                timeout_seconds=benchmark.timeout_seconds,
                max_output_bytes=benchmark.max_output_bytes,
                solution_filename=instance.submission_filename,
                result_filename=instance.result_filename,
            )
        finally:
            server.stop()
            final_snapshot = rack.world.snapshot()
            rack.close()
        report = grade_run(
            process.result,
            journal.events(),
            spec,
            final_snapshot,
            infrastructure_ok=True,
        )
        if process.status != "completed":
            report = dataclass_replace_status(report, process.status)
        return WorldExecution(process=process, report=report)


def run_suite(
    *,
    benchmark: RunSettings,
    instance: InstanceSettings,
    specs: Mapping[str, WorldSpec],
    candidate_path: Path,
    execute: Callable[..., object] = run_world,
) -> tuple[object, ...]:
    missing = [world_id for world_id in benchmark.fixed_worlds if world_id not in specs]
    if missing:
        raise ValueError(f"missing fixed world specs: {', '.join(missing)}")
    return tuple(
        execute(
            benchmark=benchmark,
            instance=instance,
            spec=specs[world_id],
            candidate_path=candidate_path,
        )
        for world_id in benchmark.fixed_worlds
    )


def run_full_suite(
    *,
    benchmark: RunSettings,
    instance: InstanceSettings,
    candidate_path: Path,
    world_directory: Path,
    repeated_base_seed: int = 20_000,
) -> EvaluationReport:
    fixed_specs = load_world_specs(world_directory)
    fixed_executions = run_suite(
        benchmark=benchmark,
        instance=instance,
        specs=fixed_specs,
        candidate_path=candidate_path,
    )
    repeated_executions = tuple(
        run_world(
            benchmark=benchmark,
            instance=instance,
            spec=spec,
            candidate_path=candidate_path,
        )
        for spec in repeated_specs(
            benchmark.repeated_worlds, base_seed=repeated_base_seed
        )
    )
    return aggregate_reports(
        tuple(execution.report for execution in fixed_executions),
        tuple(execution.report for execution in repeated_executions),
    )


def dataclass_replace_status(report: WorldReport, status: str) -> WorldReport:
    from dataclasses import replace

    return replace(report, status=status, strict_pass=False)
