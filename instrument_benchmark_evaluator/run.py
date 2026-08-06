from __future__ import annotations

import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from sources.pyvisa.pyvisa_dut_validation_v1.gateway.journal import EventJournal
from sources.pyvisa.pyvisa_dut_validation_v1.gateway.server import GatewayServer
from sources.pyvisa.pyvisa_dut_validation_v1.instruments import InstrumentRack
from sources.pyvisa.pyvisa_dut_validation_v1.models import WorldSnapshot, WorldSpec
from sources.pyvisa.pyvisa_dut_validation_v1.scoring import (
    EvaluationReport,
    WorldReport,
    aggregate_reports,
    grade_run,
)
from sources.pyvisa.pyvisa_dut_validation_v1.worlds import (
    load_world_specs,
    repeated_specs,
)

from .candidate_backend import CandidateBackend, CandidateProcessResult
from .container.runner import ContainerProcessResult
from .contracts import InstanceSettings, RunSettings
from .isolation import prepare_workspace


@dataclass(frozen=True)
class WorldExecution:
    process: CandidateProcessResult
    report: WorldReport
    pre_cleanup_snapshot: WorldSnapshot
    post_cleanup_snapshot: WorldSnapshot
    forced_cleanup: bool


def run_world(
    *,
    benchmark: RunSettings,
    instance: InstanceSettings,
    spec: WorldSpec,
    candidate_path: Path,
    backend: CandidateBackend,
) -> WorldExecution:
    with tempfile.TemporaryDirectory(
        prefix="w-",
        dir=benchmark.shared_run_root,
    ) as directory:
        root = Path(directory)
        root.chmod(0o755)
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
        endpoint.chmod(0o666)
        try:
            process = backend.invoke(
                workspace=workspace,
                candidate_path=candidate_path,
                endpoint=endpoint,
                instance=instance,
                timeout_seconds=benchmark.timeout_seconds,
                max_output_bytes=benchmark.max_output_bytes,
                run_id=benchmark.run_id,
                world_id=spec.world_id,
            )
        finally:
            server.stop()
            final_snapshot = rack.world.snapshot()
            forced_cleanup = not final_snapshot.safe
            rack.force_safe()
            post_cleanup_snapshot = rack.world.snapshot()
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
        report = attach_runtime_evidence(
            report, process, forced_cleanup=forced_cleanup
        )
        return WorldExecution(
            process=process,
            report=report,
            pre_cleanup_snapshot=final_snapshot,
            post_cleanup_snapshot=post_cleanup_snapshot,
            forced_cleanup=forced_cleanup,
        )


def run_suite(
    *,
    benchmark: RunSettings,
    instance: InstanceSettings,
    specs: Mapping[str, WorldSpec],
    candidate_path: Path,
    backend: CandidateBackend,
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
            backend=backend,
        )
        for world_id in benchmark.fixed_worlds
    )


def run_full_suite(
    *,
    benchmark: RunSettings,
    instance: InstanceSettings,
    candidate_path: Path,
    world_directory: Path,
    backend: CandidateBackend,
    repeated_base_seed: int = 20_000,
) -> EvaluationReport:
    fixed_specs = load_world_specs(world_directory)
    fixed_executions = run_suite(
        benchmark=benchmark,
        instance=instance,
        specs=fixed_specs,
        candidate_path=candidate_path,
        backend=backend,
    )
    repeated_executions = tuple(
        run_world(
            benchmark=benchmark,
            instance=instance,
            spec=spec,
            candidate_path=candidate_path,
            backend=backend,
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
    return replace(report, status=status, strict_pass=False)


def attach_runtime_evidence(
    report: WorldReport,
    process: CandidateProcessResult,
    *,
    forced_cleanup: bool,
) -> WorldReport:
    if not isinstance(process, ContainerProcessResult):
        return replace(report, forced_cleanup=forced_cleanup)
    container = process.container_evidence
    checks = (
        container.network_mode == "none",
        container.readonly_rootfs,
        container.user == "10001:10001",
        "ALL" in container.cap_drop,
        "no-new-privileges" in container.security_options,
        container.cleanup_succeeded is True,
        bool(container.image_digest),
    )
    runtime_confidence = sum(checks) / len(checks)
    confidence = replace(
        report.evidence_confidence,
        container_runtime=runtime_confidence,
    )
    infrastructure_valid = process.status != "infrastructure_failure"
    return replace(
        report,
        evidence_confidence=confidence,
        container_evidence=container.to_dict(),
        artifact_evidence=(
            process.artifact_evidence.to_dict()
            if process.artifact_evidence is not None
            else None
        ),
        forced_cleanup=forced_cleanup,
        infrastructure_valid=infrastructure_valid,
        retry_eligible=not infrastructure_valid,
        strict_pass=report.strict_pass and infrastructure_valid,
    )
