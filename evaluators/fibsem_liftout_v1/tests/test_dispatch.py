from __future__ import annotations

from pathlib import Path

import pytest

from evaluators.fibsem_liftout_v1.instrumented_microscope import (
    OperationDispatcher,
    RejectedOperation,
)
from evaluators.fibsem_liftout_v1.journal import EventJournal
from evaluators.fibsem_liftout_v1.models import ScenarioSpec


ROOT = Path(__file__).resolve().parents[3]
NOMINAL = ROOT.parent / "instance" / "fibsem_liftout_v1" / "scenarios" / "nominal.json"


class FakeBackend:
    def __init__(self) -> None:
        self.stage = [0.0, 0.0, 0.0]
        self.needle = [-28.0, 0.0, 7.0]
        self.inserted = False
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.next_pattern_status = "completed"
        self.patterns: dict[str, str] = {}

    def semantic_state(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "needle": self.needle,
            "inserted": self.inserted,
            "calls": len(self.calls),
        }

    def invoke(self, operation: str, arguments: dict[str, object]) -> object:
        self.calls.append((operation, arguments))
        if operation == "get_stage_position":
            return self._pose(self.stage)
        if operation == "move_stage":
            self.stage = self._move(self.stage, arguments)
            return self._pose(self.stage)
        if operation == "insert_manipulator":
            self.inserted = True
            if "position_um" in arguments:
                self.needle = list(arguments["position_um"])  # type: ignore[arg-type]
            return self._pose(self.needle)
        if operation == "move_manipulator":
            self.needle = self._move(self.needle, arguments)
            return self._pose(self.needle)
        if operation == "retract_manipulator":
            self.inserted = False
            self.needle = [-28.0, 0.0, 7.0]
            return self._pose(self.needle)
        if operation in {"run_cut", "run_deposition"}:
            operation_id = f"op-{len(self.calls)}"
            self.patterns[operation_id] = self.next_pattern_status
            return {"operation_id": operation_id, "status": self.next_pattern_status}
        if operation == "pattern_status":
            operation_id = arguments["operation_id"]
            self.patterns[operation_id] = self.next_pattern_status  # type: ignore[index]
            return {"operation_id": operation_id, "status": self.next_pattern_status}
        if operation == "stop_pattern":
            operation_id = arguments["operation_id"]
            self.patterns[operation_id] = "stopped"  # type: ignore[index]
            return {"operation_id": operation_id, "status": "stopped"}
        if operation == "checkpoint":
            return {
                "step_id": arguments["step_id"],
                "world_id": "nominal",
                "journal_sequence": len(self.calls),
                "artifact_digest": "a" * 64,
            }
        if operation == "acquire_image":
            return {
                "beam": arguments["beam"],
                "width": 1,
                "height": 1,
                "pixels_base64": "AA==",
                "metadata": {},
            }
        if operation in {"ping", "capabilities", "get_manipulator_state"}:
            return {"ok": True}
        return None

    @staticmethod
    def motion_is_safe(kind: str, target_um: tuple[float, float, float]) -> bool:
        return max(abs(value) for value in target_um) < 5000

    @staticmethod
    def _move(current: list[float], arguments: dict[str, object]) -> list[float]:
        requested = list(arguments["position_um"])  # type: ignore[arg-type]
        if arguments["relative"]:
            return [left + right for left, right in zip(current, requested, strict=True)]
        return requested

    @staticmethod
    def _pose(position: list[float]) -> dict[str, object]:
        return {
            "relative_to": "world",
            "position_um": position,
            "orientation_degrees": [0.0, 0.0, 0.0],
        }


def dispatcher() -> tuple[OperationDispatcher, FakeBackend, EventJournal]:
    backend = FakeBackend()
    journal = EventJournal("run", "nominal")
    return OperationDispatcher(backend, ScenarioSpec.from_path(NOMINAL), journal), backend, journal


def pattern(purpose: str, frame: str, center: list[float]) -> dict[str, object]:
    return {
        "pattern": {
            "purpose": purpose,
            "frame": frame,
            "center_um": center,
            "size_um": [1.0, 1.0, 1.0],
            "rotation_degrees": 0.0,
        }
    }


def complete_preflight(subject: OperationDispatcher) -> None:
    subject.dispatch("ping", {})
    subject.dispatch("capabilities", {})
    subject.dispatch("acquire_image", {"beam": "SEM"})
    subject.dispatch("acquire_image", {"beam": "FIB"})
    subject.dispatch("move_stage", {"position_um": [1.0, 0.0, 0.0], "relative": True})
    subject.dispatch("move_stage", {"position_um": [-1.0, 0.0, 0.0], "relative": True})
    subject.dispatch("insert_manipulator", {})
    subject.dispatch("move_manipulator", {"position_um": [1.0, 0.0, 0.0], "relative": True})
    subject.dispatch("retract_manipulator", {})
    subject.dispatch("run_cut", pattern("preflight_cut", "coupon", [0.0, 0.0, 2.0]))
    subject.dispatch(
        "run_deposition", pattern("preflight_deposition", "coupon", [0.0, 0.0, 2.0])
    )


def test_dispatch_rejects_private_unknown_and_malformed_operation() -> None:
    subject, backend, journal = dispatcher()

    for operation in ("_update_mesh", "openfibsem.workflow", "unknown"):
        with pytest.raises(RejectedOperation, match="not allowed"):
            subject.dispatch(operation, {})

    assert backend.calls == []
    assert [event.kind for event in journal.events].count("rpc.rejected") == 3

    with pytest.raises(RejectedOperation, match="invalid|oversized"):
        subject.dispatch(
            "move_stage", {"position_um": object(), "relative": False}
        )
    assert journal.events[-1].kind == "rpc.rejected"


def test_dispatch_requires_preflight_before_task_roi_and_records_state_hashes() -> None:
    subject, backend, journal = dispatcher()
    with pytest.raises(RejectedOperation, match="Preflight"):
        subject.dispatch("run_cut", pattern("u_cut", "sample", [0.0, 0.0, 0.0]))

    complete_preflight(subject)
    result = subject.dispatch("run_cut", pattern("u_cut", "sample", [0.0, 0.0, 0.0]))

    assert result["status"] == "completed"  # type: ignore[index]
    completed = [event for event in journal.events if event.kind == "rpc.completed"][-1]
    assert completed.fields["before_state_hash"] != completed.fields["after_state_hash"]
    assert completed.fields["request_id"].startswith("direct-")
    assert completed.fields["details"] == {"pattern_purpose": "u_cut"}
    assert subject.preflight_complete


def test_dispatch_rejects_motion_limit_pattern_escape_and_wrong_checkpoint_order() -> None:
    subject, _, _ = dispatcher()
    with pytest.raises(RejectedOperation, match="stage movement limit"):
        subject.dispatch(
            "move_stage", {"position_um": [1200.1, 0.0, 0.0], "relative": True}
        )
    with pytest.raises(RejectedOperation, match="work envelope"):
        subject.dispatch(
            "run_cut", pattern("preflight_cut", "coupon", [100.0, 0.0, 0.0])
        )

    complete_preflight(subject)
    with pytest.raises(RejectedOperation, match="step_1"):
        subject.dispatch("checkpoint", {"step_id": "step_2"})
    first = subject.dispatch("checkpoint", {"step_id": "step_1", "summary": {"ok": True}})
    assert first["step_id"] == "step_1"  # type: ignore[index]


def test_dispatch_rejects_wrong_pattern_polarity_and_frame() -> None:
    subject, _, _ = dispatcher()
    with pytest.raises(RejectedOperation, match="purpose"):
        subject.dispatch(
            "run_deposition", pattern("preflight_cut", "coupon", [0.0, 0.0, 2.0])
        )
    with pytest.raises(RejectedOperation, match="frame"):
        subject.dispatch(
            "run_cut", pattern("preflight_cut", "sample", [0.0, 0.0, 0.0])
        )


def test_dispatch_tracks_pattern_lifecycle_and_requires_idle_checkpoint() -> None:
    subject, backend, _ = dispatcher()
    complete_preflight(subject)
    backend.next_pattern_status = "running"
    receipt = subject.dispatch(
        "run_cut", pattern("u_cut", "sample", [0.0, 0.0, 0.0])
    )
    operation_id = receipt["operation_id"]  # type: ignore[index]

    with pytest.raises(RejectedOperation, match="active pattern"):
        subject.dispatch("checkpoint", {"step_id": "step_1"})
    with pytest.raises(RejectedOperation, match="unknown operation ID"):
        subject.dispatch("pattern_status", {"operation_id": "op-forged"})

    backend.next_pattern_status = "completed"
    subject.dispatch("pattern_status", {"operation_id": operation_id})
    assert subject.dispatch("checkpoint", {"step_id": "step_1"})["step_id"] == "step_1"  # type: ignore[index]
