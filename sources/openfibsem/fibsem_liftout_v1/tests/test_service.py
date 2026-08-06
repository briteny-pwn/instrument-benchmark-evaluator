from __future__ import annotations

from pathlib import Path
import json
import stat

from sources.openfibsem.fibsem_liftout_v1.checkpoint_exporter import CheckpointExporter
from sources.openfibsem.fibsem_liftout_v1.journal import EventJournal
from sources.openfibsem.fibsem_liftout_v1.models import ScenarioSpec
from sources.openfibsem.fibsem_liftout_v1.service import FibsemService
from sources.openfibsem.fibsem_liftout_v1.tests.fakes import RecordingBackend


ROOT = Path(__file__).resolve().parents[4]
NOMINAL = ROOT.parent / "instance" / "sources" / "openfibsem" / "fibsem_liftout_v1" / "scenarios" / "nominal.json"


def make_service(backend: RecordingBackend, root: Path) -> FibsemService:
    journal = EventJournal("run", "nominal")
    return FibsemService(
        backend,
        ScenarioSpec.from_path(NOMINAL),
        journal,
        CheckpointExporter(root),
    )


def test_checkpoint_freezes_before_imaging_and_export(tmp_path: Path) -> None:
    backend = RecordingBackend()
    service = make_service(backend, tmp_path)

    receipt = service.checkpoint("step_1")

    assert backend.calls[:2] == ["freeze_snapshot", "acquire_checkpoint_images"]
    assert receipt["step_id"] == "step_1"
    assert receipt["artifact_digest"]

    summary = service.finalize(outcome="candidate_incomplete", forced=True)
    checkpoint = summary["checkpoint_evidence"]["step_1"]
    assert checkpoint["artifact_digest"] == receipt["artifact_digest"]
    assert checkpoint["geometry"]["canonical_geometry_hash"]
    assert checkpoint["artifact_path"] == "artifacts/nominal/step_1"
    manifest = json.loads(
        (tmp_path / checkpoint["artifact_path"] / "checkpoint.json").read_text()
    )
    assert manifest["scenario_digest"]
    assert manifest["geometry"] == checkpoint["geometry"]


def test_finalize_records_pre_and_post_forced_cleanup_state(tmp_path: Path) -> None:
    backend = RecordingBackend()
    service = make_service(backend, tmp_path)

    result = service.finalize(outcome="candidate_crash", forced=True)

    assert result["cleanup"]["forced"] is True
    assert result["cleanup"]["pre_cleanup"]["safe"] is False
    assert result["cleanup"]["post_cleanup"]["safe"] is True
    assert backend.calls[-3:] == ["cancel", "force_safe", "close"]
    assert (tmp_path / "journal.jsonl").is_file()
    assert (tmp_path / "service-summary.json").is_file()
    assert stat.S_IMODE((tmp_path / "service-summary.json").stat().st_mode) == 0o644
    assert stat.S_IMODE((tmp_path / "journal.jsonl").stat().st_mode) == 0o644
    assert stat.S_IMODE((tmp_path / "journal-summary.json").stat().st_mode) == 0o644


def test_candidate_protocol_failure_is_journaled_as_rejected_not_infrastructure(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend()
    service = make_service(backend, tmp_path)

    service.record_protocol_rejection("ProtocolError")
    summary = service.finalize(outcome="candidate_failure", forced=True)

    rejected = [
        event for event in service.journal.events if event.kind == "rpc.rejected"
    ]
    assert rejected[0].fields == {
        "reason": "candidate protocol not allowed",
        "error_type": "ProtocolError",
    }
    assert summary["outcome"] == "candidate_failure"


def test_trusted_rpc_failure_is_reported_as_infrastructure(tmp_path: Path) -> None:
    backend = RecordingBackend()
    service = make_service(backend, tmp_path)
    service.journal.append(
        "rpc.failed",
        request_id="req-00000001",
        operation="acquire_image",
        error_type="OpenFibsemRuntimeError",
    )

    summary = service.finalize(outcome="candidate_incomplete", forced=True)

    assert summary["outcome"] == "infrastructure_failure"
    assert service.journal.events[-1].fields["outcome"] == "infrastructure_failure"
