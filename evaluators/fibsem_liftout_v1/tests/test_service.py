from __future__ import annotations

from pathlib import Path

from evaluators.fibsem_liftout_v1.checkpoint_exporter import CheckpointExporter
from evaluators.fibsem_liftout_v1.journal import EventJournal
from evaluators.fibsem_liftout_v1.models import ScenarioSpec
from evaluators.fibsem_liftout_v1.service import FibsemService
from evaluators.fibsem_liftout_v1.tests.fakes import RecordingBackend


ROOT = Path(__file__).resolve().parents[3]
NOMINAL = ROOT.parent / "instance" / "fibsem_liftout_v1" / "scenarios" / "nominal.json"


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
