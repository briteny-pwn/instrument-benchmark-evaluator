from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from .geometry.metrics import Bounds
from .geometry.oracle import PURPOSE_PHASES
from .journal import EventJournal, JournalError, canonical_digest
from .models import ScenarioSpec, finite, vec3
from .protocol import BrokerVisibleError


CUT_PURPOSES = {
    "preflight_cut",
    "trench",
    "polish",
    "u_cut",
    "source_separation",
    "needle_separation",
}
DEPOSITION_PURPOSES = {
    "preflight_deposition",
    "protection",
    "needle_joint",
    "target_joint",
}
STEPS = ("step_1", "step_2", "step_3", "step_4")


class RejectedOperation(BrokerVisibleError):
    """A candidate operation is outside the documented public contract."""


class Backend(Protocol):
    def semantic_state(self) -> Mapping[str, object]: ...

    def invoke(self, operation: str, arguments: dict[str, object]) -> object: ...

    def motion_is_safe(
        self, kind: str, target_um: tuple[float, float, float]
    ) -> bool: ...


@dataclass
class _PreflightState:
    ping: bool = False
    capabilities: bool = False
    beams: set[str] = field(default_factory=set)
    stage_moved: bool = False
    stage_returned: bool = False
    needle_inserted: bool = False
    needle_moved: bool = False
    needle_retracted: bool = False
    coupon_cut: bool = False
    coupon_deposition: bool = False

    @property
    def complete(self) -> bool:
        return (
            self.ping
            and self.capabilities
            and self.beams == {"SEM", "FIB"}
            and self.stage_moved
            and self.stage_returned
            and self.needle_inserted
            and self.needle_moved
            and self.needle_retracted
            and self.coupon_cut
            and self.coupon_deposition
        )


class OperationDispatcher:
    def __init__(
        self, backend: Backend, scenario: ScenarioSpec, journal: EventJournal
    ) -> None:
        self.backend = backend
        self.scenario = scenario
        self.journal = journal
        self._preflight = _PreflightState()
        self._checkpoint_index = 0
        self._direct_request_number = 0
        self._patterns: dict[str, dict[str, str]] = {}
        initial = self._state()
        self._stage_origin = self._state_position(initial, "stage")
        self._handlers = {
            "ping": self._ping,
            "capabilities": self._capabilities,
            "acquire_image": self._acquire_image,
            "get_stage_position": self._empty,
            "move_stage": self._move_stage,
            "stop_stage": self._empty,
            "get_manipulator_state": self._empty,
            "insert_manipulator": self._insert_manipulator,
            "move_manipulator": self._move_manipulator,
            "retract_manipulator": self._retract_manipulator,
            "stop_manipulator": self._empty,
            "run_cut": self._run_cut,
            "run_deposition": self._run_deposition,
            "pattern_status": self._operation_id,
            "stop_pattern": self._operation_id,
            "checkpoint": self._checkpoint,
        }

    @property
    def preflight_complete(self) -> bool:
        return self._preflight.complete

    def dispatch(
        self,
        operation: str,
        arguments: dict[str, object],
        *,
        request_id: str | None = None,
    ) -> object:
        if request_id is None:
            self._direct_request_number += 1
            request_id = f"direct-{self._direct_request_number:08d}"
        before = self._state()
        before_hash = canonical_digest(before)
        details = self._audit_details(operation, arguments)
        try:
            argument_digest = canonical_digest(arguments)
        except JournalError as exc:
            self._reject(
                request_id,
                operation,
                "invalid or oversized arguments",
                before_hash,
                details=details,
            )
            raise RejectedOperation("invalid or oversized arguments") from exc
        handler = self._handlers.get(operation)
        if handler is None:
            self._reject(
                request_id,
                operation,
                "operation is not allowed",
                before_hash,
                details=details,
            )
            raise RejectedOperation(f"operation is not allowed: {operation}")
        self.journal.append(
            "rpc.requested",
            request_id=request_id,
            operation=operation,
            argument_digest=argument_digest,
            details=details,
            before_state_hash=before_hash,
        )
        try:
            result = handler(operation, dict(arguments))
        except RejectedOperation as exc:
            self._reject(
                request_id,
                operation,
                str(exc),
                before_hash,
                argument_digest,
                details,
            )
            raise
        except (KeyError, TypeError, ValueError) as exc:
            message = "operation arguments are invalid"
            self._reject(
                request_id,
                operation,
                message,
                before_hash,
                argument_digest,
                details,
            )
            raise RejectedOperation(message) from exc
        except Exception as exc:
            self.journal.append(
                "rpc.failed",
                request_id=request_id,
                operation=operation,
                argument_digest=argument_digest,
                details=details,
                before_state_hash=before_hash,
                after_state_hash=canonical_digest(self._state()),
                error_type=type(exc).__name__,
            )
            raise
        after_hash = canonical_digest(self._state())
        self.journal.append(
            "rpc.completed",
            request_id=request_id,
            operation=operation,
            argument_digest=argument_digest,
            details=details,
            result_details=self._result_details(result),
            result_digest=canonical_digest(result),
            before_state_hash=before_hash,
            after_state_hash=after_hash,
        )
        return result

    def _reject(
        self,
        request_id: str,
        operation: str,
        reason: str,
        before_hash: str,
        argument_digest: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.journal.append(
            "rpc.rejected",
            request_id=request_id,
            operation=operation,
            argument_digest=argument_digest,
            details=dict(details or {}),
            before_state_hash=before_hash,
            after_state_hash=canonical_digest(self._state()),
            reason=reason,
        )

    @staticmethod
    def _audit_details(
        operation: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        details: dict[str, object] = {}
        if operation in {"run_cut", "run_deposition"}:
            pattern = arguments.get("pattern")
            if isinstance(pattern, Mapping) and isinstance(pattern.get("purpose"), str):
                details["pattern_purpose"] = pattern["purpose"]
        elif operation == "acquire_image" and isinstance(arguments.get("beam"), str):
            details["beam"] = arguments["beam"]
        elif operation in {"move_stage", "move_manipulator", "insert_manipulator"}:
            if "position_um" in arguments:
                details["position_um"] = _safe_audit_value(arguments["position_um"])
            if "relative" in arguments:
                details["relative"] = _safe_audit_value(arguments["relative"])
        elif operation == "checkpoint" and isinstance(arguments.get("step_id"), str):
            details["step_id"] = arguments["step_id"]
        elif operation in {"pattern_status", "stop_pattern"} and isinstance(
            arguments.get("operation_id"), str
        ):
            details["operation_id"] = arguments["operation_id"]
        return details

    @staticmethod
    def _result_details(result: object) -> dict[str, object]:
        if not isinstance(result, Mapping):
            return {}
        return {
            key: result[key]
            for key in ("operation_id", "status", "step_id", "journal_sequence")
            if key in result
        }

    def _invoke(self, operation: str, arguments: dict[str, object]) -> object:
        return self.backend.invoke(operation, arguments)

    def _ping(self, operation: str, arguments: dict[str, object]) -> object:
        self._require_exact(arguments, set())
        result = self._invoke(operation, arguments)
        self._preflight.ping = True
        return result

    def _capabilities(self, operation: str, arguments: dict[str, object]) -> object:
        self._require_exact(arguments, set())
        result = self._invoke(operation, arguments)
        self._preflight.capabilities = True
        return result

    def _acquire_image(self, operation: str, arguments: dict[str, object]) -> object:
        self._require_exact(arguments, {"beam"})
        beam = arguments["beam"]
        if beam not in {"SEM", "FIB"}:
            raise RejectedOperation("image beam must be SEM or FIB")
        result = self._invoke(operation, arguments)
        assert isinstance(beam, str)
        self._preflight.beams.add(beam)
        return result

    def _empty(self, operation: str, arguments: dict[str, object]) -> object:
        self._require_exact(arguments, set())
        return self._invoke(operation, arguments)

    def _move_stage(self, operation: str, arguments: dict[str, object]) -> object:
        target = self._validate_motion("stage", arguments)
        result = self._invoke(operation, arguments)
        if not _close(target, self._stage_origin):
            self._preflight.stage_moved = True
        if self._preflight.stage_moved and _close(target, self._stage_origin):
            self._preflight.stage_returned = True
        return result

    def _insert_manipulator(
        self, operation: str, arguments: dict[str, object]
    ) -> object:
        if set(arguments) not in (set(), {"position_um"}):
            raise RejectedOperation("insert manipulator arguments are invalid")
        if "position_um" in arguments:
            position = vec3(arguments["position_um"], "manipulator position")
            if not self.backend.motion_is_safe("manipulator", position):
                raise RejectedOperation("manipulator motion is unsafe")
        result = self._invoke(operation, arguments)
        self._preflight.needle_inserted = True
        return result

    def _move_manipulator(
        self, operation: str, arguments: dict[str, object]
    ) -> object:
        if not self._preflight.needle_inserted and not self.preflight_complete:
            raise RejectedOperation("manipulator must be inserted before movement")
        self._validate_motion("manipulator", arguments)
        result = self._invoke(operation, arguments)
        self._preflight.needle_moved = True
        return result

    def _retract_manipulator(
        self, operation: str, arguments: dict[str, object]
    ) -> object:
        self._require_exact(arguments, set())
        result = self._invoke(operation, arguments)
        if self._preflight.needle_inserted:
            self._preflight.needle_retracted = True
        return result

    def _run_cut(self, operation: str, arguments: dict[str, object]) -> object:
        purpose = self._validate_pattern(arguments, CUT_PURPOSES)
        if purpose != "preflight_cut" and not self.preflight_complete:
            raise RejectedOperation("Preflight must complete before task ROI operations")
        result = self._invoke(operation, arguments)
        return self._record_new_pattern(result, purpose, operation)

    def _run_deposition(
        self, operation: str, arguments: dict[str, object]
    ) -> object:
        purpose = self._validate_pattern(arguments, DEPOSITION_PURPOSES)
        if purpose != "preflight_deposition" and not self.preflight_complete:
            raise RejectedOperation("Preflight must complete before task ROI operations")
        result = self._invoke(operation, arguments)
        return self._record_new_pattern(result, purpose, operation)

    def _operation_id(self, operation: str, arguments: dict[str, object]) -> object:
        self._require_exact(arguments, {"operation_id"})
        operation_id = arguments["operation_id"]
        if not isinstance(operation_id, str) or not operation_id:
            raise RejectedOperation("operation ID is invalid")
        if operation_id not in self._patterns:
            raise RejectedOperation("unknown operation ID")
        result = self._invoke(operation, arguments)
        receipt_id, status = self._receipt(result)
        if receipt_id != operation_id:
            raise RuntimeError("backend pattern receipt identity mismatch")
        if operation == "stop_pattern" and status not in {"stopped", "completed", "failed"}:
            raise RuntimeError("backend did not stop the pattern")
        self._patterns[operation_id]["status"] = status
        if status == "completed":
            self._complete_pattern_effect(self._patterns[operation_id]["purpose"])
        return result

    def _checkpoint(self, operation: str, arguments: dict[str, object]) -> object:
        if set(arguments) not in ({"step_id"}, {"step_id", "summary"}):
            raise RejectedOperation("checkpoint arguments are invalid")
        if not self.preflight_complete:
            raise RejectedOperation("Preflight must complete before checkpoint")
        if any(
            record["status"] in {"queued", "running"}
            for record in self._patterns.values()
        ):
            raise RejectedOperation("active pattern must finish before checkpoint")
        if self._checkpoint_index >= len(STEPS):
            raise RejectedOperation("all checkpoints have already been recorded")
        expected = STEPS[self._checkpoint_index]
        if arguments["step_id"] != expected:
            raise RejectedOperation(f"next checkpoint is {expected}")
        summary = arguments.get("summary")
        if summary is not None and not isinstance(summary, dict):
            raise RejectedOperation("checkpoint summary must be an object")
        result = self._invoke(operation, arguments)
        self._checkpoint_index += 1
        self.journal.append("checkpoint.frozen", step_id=expected)
        return result

    def _record_new_pattern(
        self, result: object, purpose: str, operation: str
    ) -> object:
        operation_id, status = self._receipt(result)
        if operation_id in self._patterns:
            raise RuntimeError("backend reused a pattern operation ID")
        self._patterns[operation_id] = {
            "purpose": purpose,
            "operation": operation,
            "status": status,
        }
        if status == "completed":
            self._complete_pattern_effect(purpose)
        return result

    @staticmethod
    def _receipt(result: object) -> tuple[str, str]:
        if not isinstance(result, Mapping) or set(result) != {"operation_id", "status"}:
            raise RuntimeError("backend pattern receipt is invalid")
        operation_id, status = result["operation_id"], result["status"]
        if not isinstance(operation_id, str) or not operation_id:
            raise RuntimeError("backend pattern operation ID is invalid")
        if status not in {"queued", "running", "completed", "stopped", "failed"}:
            raise RuntimeError("backend pattern status is invalid")
        assert isinstance(status, str)
        return operation_id, status

    def _complete_pattern_effect(self, purpose: str) -> None:
        if purpose == "preflight_cut":
            self._preflight.coupon_cut = True
        elif purpose == "preflight_deposition":
            self._preflight.coupon_deposition = True

    def _validate_motion(
        self, kind: str, arguments: dict[str, object]
    ) -> tuple[float, float, float]:
        self._require_exact(arguments, {"position_um", "relative"})
        relative = arguments["relative"]
        if not isinstance(relative, bool):
            raise RejectedOperation("motion relative flag must be boolean")
        requested = vec3(arguments["position_um"], f"{kind} position")
        state_key = "stage" if kind == "stage" else "needle"
        current = self._state_position(self._state(), state_key)
        if current is None:
            raise RejectedOperation(f"{kind} state is unavailable")
        target = tuple(
            left + right for left, right in zip(current, requested, strict=True)
        ) if relative else requested
        delta = tuple(
            target_value - current_value
            for target_value, current_value in zip(target, current, strict=True)
        )
        limits = self.scenario.data["limits"]
        if not isinstance(limits, Mapping):
            raise RejectedOperation("scenario movement limits are invalid")
        limit_name = "stage_delta_um" if kind == "stage" else "manipulator_delta_um"
        maximum = vec3(limits.get(limit_name), f"{kind} movement limit")
        if any(abs(value) > bound for value, bound in zip(delta, maximum, strict=True)):
            raise RejectedOperation(f"{kind} movement limit exceeded")
        if not self.backend.motion_is_safe(kind, target):
            raise RejectedOperation(f"{kind} motion is unsafe")
        return target

    def _validate_pattern(
        self, arguments: dict[str, object], allowed_purposes: set[str]
    ) -> str:
        self._require_exact(arguments, {"pattern"})
        value = arguments["pattern"]
        required = {"purpose", "frame", "center_um", "size_um", "rotation_degrees"}
        if not isinstance(value, dict) or set(value) != required:
            raise RejectedOperation("pattern fields are invalid")
        purpose, frame = value["purpose"], value["frame"]
        if not isinstance(purpose, str) or purpose not in allowed_purposes:
            raise RejectedOperation("pattern purpose is invalid for this operation")
        phase = PURPOSE_PHASES[purpose]
        envelope = self._envelope(phase)
        if not isinstance(frame, str) or frame != envelope[0]:
            raise RejectedOperation("pattern frame does not match its work envelope")
        center = vec3(value["center_um"], "pattern center")
        size = vec3(value["size_um"], "pattern size")
        rotation = finite(value["rotation_degrees"], "pattern rotation")
        patterning = self.scenario.data["patterning"]
        if not isinstance(patterning, Mapping):
            raise RejectedOperation("scenario pattern limits are invalid")
        minimum = finite(patterning.get("minimum_feature_um"), "minimum feature")
        maximum = finite(patterning.get("maximum_feature_um"), "maximum feature")
        if any(value < minimum or value > maximum for value in size):
            raise RejectedOperation("pattern feature size is outside declared limits")
        radians = math.radians(rotation)
        extent = (
            abs(math.cos(radians)) * size[0] + abs(math.sin(radians)) * size[1],
            abs(math.sin(radians)) * size[0] + abs(math.cos(radians)) * size[1],
            size[2],
        )
        origin = self.scenario.world_position(frame)
        world_center = tuple(
            base + offset for base, offset in zip(origin, center, strict=True)
        )
        pattern_bounds = Bounds(
            tuple(value - width / 2 for value, width in zip(world_center, extent, strict=True)),  # type: ignore[arg-type]
            tuple(value + width / 2 for value, width in zip(world_center, extent, strict=True)),  # type: ignore[arg-type]
        )
        if not envelope[1].contains(pattern_bounds):
            raise RejectedOperation("pattern escapes its declared work envelope")
        return purpose

    def _envelope(self, phase: str) -> tuple[str, Bounds]:
        envelopes = self.scenario.data["work_envelopes"]
        if not isinstance(envelopes, Mapping) or not isinstance(envelopes.get(phase), Mapping):
            raise RejectedOperation("scenario work envelope is invalid")
        value = envelopes[phase]
        assert isinstance(value, Mapping)
        frame = value.get("frame")
        if not isinstance(frame, str):
            raise RejectedOperation("scenario work envelope frame is invalid")
        center = vec3(value.get("center_um"), "work envelope center")
        size = vec3(value.get("size_um"), "work envelope size")
        origin = self.scenario.world_position(frame)
        world_center = tuple(
            base + offset for base, offset in zip(origin, center, strict=True)
        )
        return frame, Bounds(
            tuple(value - width / 2 for value, width in zip(world_center, size, strict=True)),  # type: ignore[arg-type]
            tuple(value + width / 2 for value, width in zip(world_center, size, strict=True)),  # type: ignore[arg-type]
        )

    @staticmethod
    def _require_exact(arguments: dict[str, object], expected: set[str]) -> None:
        if set(arguments) != expected:
            raise RejectedOperation("operation arguments are invalid")

    def _state(self) -> Mapping[str, object]:
        state = self.backend.semantic_state()
        if not isinstance(state, Mapping):
            raise RuntimeError("backend semantic state must be an object")
        return state

    @staticmethod
    def _state_position(
        state: Mapping[str, object], key: str
    ) -> tuple[float, float, float] | None:
        value = state.get(key)
        try:
            return vec3(value, f"backend {key} state")
        except ValueError:
            return None


def _close(
    first: tuple[float, float, float] | None,
    second: tuple[float, float, float] | None,
    tolerance: float = 1e-6,
) -> bool:
    if first is None or second is None:
        return False
    return all(abs(left - right) <= tolerance for left, right in zip(first, second, strict=True))


def _safe_audit_value(value: object) -> object:
    try:
        canonical_digest(value)
    except JournalError:
        return "<invalid>"
    return value
