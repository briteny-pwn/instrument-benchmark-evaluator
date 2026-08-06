from __future__ import annotations

import json
import os
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from .backend import OpenFibsemBackend
from .checkpoint_exporter import CheckpointExporter
from .geometry.oracle import GeometryOracle
from .instrumented_microscope import OperationDispatcher
from .journal import EventJournal, canonical_digest
from .models import ScenarioSpec
from .protocol import FibsemBroker, ProtocolError


class ServiceStopRequested(Exception):
    """The trusted outer evaluator requested graceful finalization."""


@dataclass(frozen=True)
class FrozenCheckpoint:
    snapshot: object
    geometry: object
    artifacts: object


class ServiceBackend(Protocol):
    def semantic_state(self) -> Mapping[str, object]: ...

    def invoke(self, operation: str, arguments: dict[str, object]) -> object: ...

    def motion_is_safe(
        self, kind: str, target_um: tuple[float, float, float]
    ) -> bool: ...

    def freeze_snapshot(self, step_id: str): ...

    def acquire_checkpoint_images(self) -> dict[str, tuple[int, int, bytes]]: ...

    def cancel(self) -> None: ...

    def force_safe(self) -> None: ...

    def close(self) -> None: ...


class FibsemService:
    def __init__(
        self,
        backend: ServiceBackend,
        scenario: ScenarioSpec,
        journal: EventJournal,
        exporter: CheckpointExporter,
    ) -> None:
        self.backend = backend
        self.scenario = scenario
        self.journal = journal
        self.exporter = exporter
        self.checkpoints: list[str] = []
        self.frozen_checkpoints: dict[str, FrozenCheckpoint] = {}
        self._finalized = False

    def semantic_state(self) -> Mapping[str, object]:
        return self.backend.semantic_state()

    def motion_is_safe(
        self, kind: str, target_um: tuple[float, float, float]
    ) -> bool:
        return self.backend.motion_is_safe(kind, target_um)

    def invoke(self, operation: str, arguments: dict[str, object]) -> object:
        if operation == "checkpoint":
            step_id = arguments["step_id"]
            assert isinstance(step_id, str)
            summary = arguments.get("summary")
            assert summary is None or isinstance(summary, dict)
            try:
                return self.checkpoint(step_id, summary)
            except Exception as exc:
                raise RuntimeError("trusted checkpoint export failed") from exc
        return self.backend.invoke(operation, arguments)

    def record_protocol_rejection(self, error_type: str) -> None:
        self.journal.append(
            "rpc.rejected",
            reason="candidate protocol not allowed",
            error_type=error_type,
        )

    def checkpoint(
        self, step_id: str, summary: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        if self._finalized:
            raise RuntimeError("service is already finalized")
        self.journal.append(
            "checkpoint.freeze_requested",
            step_id=step_id,
            candidate_summary=dict(summary or {}),
        )
        snapshot = self.backend.freeze_snapshot(step_id)
        images = self.backend.acquire_checkpoint_images()
        metrics = GeometryOracle(self.scenario).evaluate(snapshot)
        evidence = self.exporter.export(
            snapshot,
            images,
            world_id=self.scenario.scenario_id,
            journal_sequence=self.journal.sequence,
            journal_hash=self.journal.head_hash,
            scenario_digest=canonical_digest(self.scenario.to_dict()),
            geometry_metrics=metrics.to_dict(),
        )
        self.checkpoints.append(step_id)
        self.frozen_checkpoints[step_id] = FrozenCheckpoint(
            snapshot=snapshot,
            geometry=metrics,
            artifacts=evidence,
        )
        self.journal.append(
            "checkpoint.exported",
            step_id=step_id,
            artifact_digest=evidence.bundle_sha256,
            geometry_hash=metrics.canonical_geometry_hash,
        )
        return {
            "step_id": step_id,
            "world_id": self.scenario.scenario_id,
            "journal_sequence": evidence.journal_sequence,
            "artifact_digest": evidence.bundle_sha256,
        }

    def finalize(self, *, outcome: str, forced: bool) -> dict[str, object]:
        if self._finalized:
            raise RuntimeError("service has already been finalized")
        self._finalized = True
        pre_cleanup = dict(self.backend.semantic_state())
        self.journal.append(
            "cleanup.started", outcome=outcome, forced=forced, state=pre_cleanup
        )
        cleanup_error: str | None = None
        try:
            self.backend.cancel()
            self.backend.force_safe()
        except Exception as exc:
            cleanup_error = type(exc).__name__
        post_cleanup = dict(self.backend.semantic_state())
        try:
            self.backend.close()
        except Exception as exc:
            cleanup_error = cleanup_error or type(exc).__name__
        cleanup = {
            "forced": forced,
            "pre_cleanup": pre_cleanup,
            "post_cleanup": post_cleanup,
            "error_type": cleanup_error,
        }
        trusted_failure = any(
            event.kind == "rpc.failed" for event in self.journal.events
        )
        terminal_outcome = (
            "cleanup_failure"
            if cleanup_error is not None
            else "infrastructure_failure"
            if trusted_failure
            else outcome
        )
        self.journal.append("run.terminal", outcome=terminal_outcome, cleanup=cleanup)
        journal_path, journal_summary = self.journal.export(self.exporter.evidence_root)
        summary: dict[str, object] = {
            "schema_version": 1,
            "run_id": self.journal.run_id,
            "world_id": self.scenario.scenario_id,
            "scenario_digest": canonical_digest(self.scenario.to_dict()),
            "outcome": terminal_outcome,
            "checkpoints": list(self.checkpoints),
            "checkpoint_evidence": {
                step_id: {
                    "geometry": frozen.geometry.to_dict(),
                    "artifact_digest": frozen.artifacts.bundle_sha256,
                    "artifact_path": (
                        f"artifacts/{self.scenario.scenario_id}/{step_id}"
                    ),
                }
                for step_id, frozen in self.frozen_checkpoints.items()
            },
            "journal": {
                "path": journal_path.name,
                "summary_path": journal_summary.name,
                "head_hash": self.journal.head_hash,
                "event_count": self.journal.sequence,
            },
            "cleanup": cleanup,
        }
        _atomic_json(self.exporter.evidence_root / "service-summary.json", summary)
        return summary


def run_service(
    world_path: Path,
    endpoint: Path,
    evidence_root: Path,
    run_id: str,
    *,
    expected_peer_uid: int = 10001,
) -> dict[str, object]:
    scenario = ScenarioSpec.from_path(world_path)
    backend = OpenFibsemBackend(scenario)
    journal = EventJournal(run_id, scenario.scenario_id)
    service = FibsemService(
        backend, scenario, journal, CheckpointExporter(evidence_root)
    )
    dispatcher = OperationDispatcher(service, scenario, journal)
    broker = FibsemBroker(dispatcher, expected_peer_uid=expected_peer_uid)
    socket_path = Path(endpoint)
    if not socket_path.is_absolute():
        raise ValueError("service endpoint must be absolute")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists() or socket_path.is_symlink():
        raise FileExistsError(f"service endpoint already exists: {socket_path}")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    forced = False
    outcome = "completed"
    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o666)
        server.listen(1)
        connection, _ = server.accept()
        with connection:
            broker.serve_connection(connection)
        if service.checkpoints != ["step_1", "step_2", "step_3", "step_4"]:
            outcome = "candidate_incomplete"
            forced = True
    except ProtocolError as exc:
        service.record_protocol_rejection(type(exc).__name__)
        outcome = "candidate_failure"
        forced = True
    except ServiceStopRequested:
        outcome = "candidate_incomplete"
        forced = True
    except Exception:
        outcome = "infrastructure_failure"
        forced = True
        raise
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)
        if not service._finalized:
            summary = service.finalize(outcome=outcome, forced=forced)
    return summary


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
