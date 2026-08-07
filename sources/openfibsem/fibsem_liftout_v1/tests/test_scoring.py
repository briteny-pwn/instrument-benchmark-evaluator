from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sources.openfibsem.fibsem_liftout_v1.geometry.oracle import GeometryMetrics
from sources.openfibsem.fibsem_liftout_v1.journal import EventJournal
from sources.openfibsem.fibsem_liftout_v1.models import ScenarioSpec
from sources.openfibsem.fibsem_liftout_v1.scoring import (
    CheckpointEvidence,
    RuntimeEvidence,
    TerminalEvidence,
    aggregate_worlds,
    grade_world,
)


ROOT = Path(__file__).resolve().parents[4]
NOMINAL = ROOT.parent / "instance" / "sources" / "openfibsem" / "fibsem_liftout_v1" / "scenarios" / "nominal.json"


def geometry(**changes: object) -> GeometryMetrics:
    base = GeometryMetrics(
        sample_to_source=False,
        sample_to_needle=False,
        sample_to_target=False,
        needle_joint_section_um=1.0,
        target_joint_section_um=1.0,
        sample_component_count=1,
        total_sample_fraction=1.0,
        retained_sample_fraction=1.0,
        sample_position_error_um=0.0,
        sample_orientation_error_degrees=0.0,
        sample_integrity_step_1=True,
        sample_integrity_final=True,
        changes_within_work_envelopes=True,
        envelope_violations=(),
        needle_retraction_distance_um=100.0,
        needle_safely_retracted=True,
        collision=False,
        simulator_idle=True,
        canonical_geometry_hash="a" * 64,
    )
    return replace(base, **changes)


def checkpoints() -> dict[str, CheckpointEvidence]:
    return {
        "step_1": CheckpointEvidence(
            "step_1",
            geometry(sample_to_source=True, needle_joint_section_um=0.0),
            True,
            "1" * 64,
        ),
        "step_2": CheckpointEvidence(
            "step_2",
            geometry(sample_to_needle=True),
            True,
            "2" * 64,
        ),
        "step_3": CheckpointEvidence(
            "step_3",
            geometry(
                sample_to_needle=True,
                sample_to_target=True,
                needle_safely_retracted=False,
            ),
            True,
            "3" * 64,
        ),
        "step_4": CheckpointEvidence(
            "step_4",
            geometry(sample_to_target=True),
            True,
            "4" * 64,
        ),
    }


def append_rpc(
    journal: EventJournal,
    operation: str,
    *,
    details: dict[str, object] | None = None,
    result: dict[str, object] | None = None,
) -> None:
    journal.append(
        "rpc.completed",
        request_id=f"test-{journal.sequence + 1}",
        operation=operation,
        argument_digest="a" * 64,
        details=details or {},
        result_details=result or {},
        result_digest="b" * 64,
        before_state_hash="c" * 64,
        after_state_hash="d" * 64,
    )


def valid_journal(world_id: str = "nominal") -> EventJournal:
    journal = EventJournal("run", world_id)
    append_rpc(journal, "ping")
    append_rpc(journal, "capabilities")
    append_rpc(journal, "acquire_image", details={"beam": "SEM"})
    append_rpc(journal, "acquire_image", details={"beam": "FIB"})
    append_rpc(journal, "move_stage", details={"position_um": [1, 0, 0], "relative": True})
    append_rpc(journal, "move_stage", details={"position_um": [-1, 0, 0], "relative": True})
    append_rpc(journal, "insert_manipulator")
    append_rpc(journal, "move_manipulator", details={"position_um": [1, 0, 0]})
    append_rpc(journal, "retract_manipulator")
    append_rpc(
        journal,
        "run_cut",
        details={"pattern_purpose": "preflight_cut"},
        result={"operation_id": "pre-cut", "status": "completed"},
    )
    append_rpc(
        journal,
        "run_deposition",
        details={"pattern_purpose": "preflight_deposition"},
        result={"operation_id": "pre-dep", "status": "completed"},
    )
    append_rpc(
        journal,
        "run_cut",
        details={"pattern_purpose": "u_cut"},
        result={"operation_id": "u-cut", "status": "completed"},
    )
    journal.append("checkpoint.exported", step_id="step_1")
    append_rpc(journal, "insert_manipulator")
    append_rpc(
        journal,
        "run_deposition",
        details={"pattern_purpose": "needle_joint"},
        result={"operation_id": "needle-dep", "status": "completed"},
    )
    append_rpc(
        journal,
        "run_cut",
        details={"pattern_purpose": "source_separation"},
        result={"operation_id": "source-cut", "status": "completed"},
    )
    append_rpc(journal, "move_manipulator", details={"position_um": [-1, 0, 0]})
    journal.append("checkpoint.exported", step_id="step_2")
    append_rpc(journal, "move_stage", details={"position_um": [-980, 0, 0]})
    append_rpc(journal, "move_manipulator", details={"position_um": [-989, 0, 6]})
    append_rpc(
        journal,
        "run_deposition",
        details={"pattern_purpose": "target_joint"},
        result={"operation_id": "target-dep", "status": "completed"},
    )
    journal.append("checkpoint.exported", step_id="step_3")
    append_rpc(
        journal,
        "run_cut",
        details={"pattern_purpose": "needle_separation"},
        result={"operation_id": "needle-cut", "status": "completed"},
    )
    append_rpc(journal, "retract_manipulator")
    journal.append("checkpoint.exported", step_id="step_4")
    journal.append("run.terminal", outcome="completed")
    return journal


def safe_terminal() -> TerminalEvidence:
    return TerminalEvidence(
        safe=True,
        simulator_idle=True,
        collision=False,
        cleanup_error=None,
    )


def valid_runtime() -> RuntimeEvidence:
    return RuntimeEvidence(
        candidate_exit_code=0,
        timed_out=False,
        forbidden_access=False,
        infrastructure_failure=False,
        candidate_uid=10001,
        simulator_uid=11001,
        isolation_verified=True,
    )


def test_nominal_evidence_scores_100() -> None:
    report = grade_world(
        ScenarioSpec.from_path(NOMINAL),
        valid_journal(),
        checkpoints(),
        safe_terminal(),
        valid_runtime(),
    )

    assert report.score == 100
    assert report.strict_pass
    assert report.step_scores == {"step_1": 20, "step_2": 25, "step_3": 25, "step_4": 20}
    assert report.artifact_score == 10
    assert all(report.strict_gates.values())


def test_missing_checkpoint_zeroes_it_and_later_steps_but_preserves_earlier_points() -> None:
    evidence = checkpoints()
    del evidence["step_3"]

    report = grade_world(
        ScenarioSpec.from_path(NOMINAL),
        valid_journal(),
        evidence,
        safe_terminal(),
        valid_runtime(),
    )

    assert report.step_scores["step_1"] == 20
    assert report.step_scores["step_2"] == 25
    assert report.step_scores["step_3"] == 0
    assert report.step_scores["step_4"] == 0
    assert not report.strict_pass


def test_partial_order_rejects_source_cut_before_needle_joint() -> None:
    journal = valid_journal()
    records = list(journal.events)
    needle = next(
        event
        for event in records
        if event.fields.get("details") == {"pattern_purpose": "needle_joint"}
    )
    source = next(
        event
        for event in records
        if event.fields.get("details") == {"pattern_purpose": "source_separation"}
    )
    needle.fields["details"]["pattern_purpose"] = "source_separation"  # type: ignore[index]
    source.fields["details"]["pattern_purpose"] = "needle_joint"  # type: ignore[index]

    report = grade_world(
        ScenarioSpec.from_path(NOMINAL),
        journal,
        checkpoints(),
        safe_terminal(),
        valid_runtime(),
    )

    assert not report.strict_gates["necessary_partial_order"]
    assert report.step_scores["step_2"] == 25
    assert not report.strict_pass


def world_report(world_id: str, *, passed: bool = True, unsafe: bool = False):
    spec = ScenarioSpec.from_dict(
        {**ScenarioSpec.from_path(NOMINAL).to_dict(), "scenario_id": world_id}
    )
    terminal = replace(safe_terminal(), safe=not unsafe)
    evidence = checkpoints()
    if not passed:
        evidence["step_4"] = replace(
            evidence["step_4"],
            geometry=replace(evidence["step_4"].geometry, sample_to_target=False),
        )
    report = grade_world(
        spec, valid_journal(world_id), evidence, terminal, valid_runtime()
    )
    candidate = {
        "container_id": "candidate",
        "image_digest": "sha256:" + "1" * 64,
        "network_mode": "none",
        "readonly_rootfs": True,
        "user": "10001:10001",
        "cap_drop": ["ALL"],
        "security_options": ["no-new-privileges"],
        "mounts": [
            {"type": "bind", "destination": name, "writable": False}
            for name in ("/workspace", "/runner", "/run/iab")
        ],
        "cleanup_attempted": True,
        "cleanup_succeeded": True,
        "candidate_status": "completed",
    }
    sim = {
        **candidate,
        "container_id": "sim",
        "image_digest": "sha256:" + "2" * 64,
        "user": "11001:11001",
        "mounts": [
            {"type": "bind", "destination": name, "writable": writable}
            for name, writable in (
                ("/run/iab/transport", True),
                ("/run/iab/evidence", True),
                ("/run/iab/world.json", False),
            )
        ],
    }
    sim.pop("candidate_status")
    return replace(
        report,
        candidate_container_evidence=candidate,
        sim_container_evidence=sim,
        trusted_evidence={
            "journal_head_hash": report.partial_order and "3" * 64,
            "journal_event_count": 100,
            "outcome": "completed",
            "forced_cleanup": False,
            "scenario_digest": "4" * 64,
        },
    )


def test_four_of_five_seeded_may_pass_but_unsafe_failure_never_passes() -> None:
    fixed = [world_report(name) for name in ("nominal", "small", "large", "needle_offset", "target_pose")]
    seeded = [world_report(f"seeded_{index:02d}") for index in range(1, 5)]
    seeded.append(world_report("seeded_05", passed=False, unsafe=True))

    report = aggregate_worlds(fixed + seeded)

    assert report.score >= 90
    assert not report.strict_pass
    assert not report.strict_gates["no_unsafe_terminal_world"]


def test_infrastructure_failure_is_retryable_without_candidate_score() -> None:
    runtime = replace(valid_runtime(), infrastructure_failure=True)
    failed = grade_world(
        ScenarioSpec.from_path(NOMINAL),
        valid_journal(),
        checkpoints(),
        safe_terminal(),
        runtime,
    )

    assert failed.score is None
    assert failed.retry_eligible
    aggregate = aggregate_worlds([failed])
    assert aggregate.score is None
    assert aggregate.retry_eligible
