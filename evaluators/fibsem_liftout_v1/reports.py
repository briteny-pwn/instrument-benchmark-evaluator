from __future__ import annotations

import copy
import math
from typing import Mapping

from .backend import OPENFIBSEM_COMMIT


class ReportError(ValueError):
    """A FIBSEM schema-version-3 report is incomplete or inconsistent."""


def validate_report(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ReportError("report must be an object")
    report = copy.deepcopy(dict(value))
    required = {
        "schema_version",
        "evaluator_id",
        "openfibsem_commit",
        "score",
        "strict_pass",
        "retry_eligible",
        "strict_gates",
        "dimension_scores",
        "evidence_confidence",
        "suite",
        "worlds",
    }
    if set(report) != required:
        raise ReportError("report fields are invalid")
    if report["schema_version"] != 3 or report["evaluator_id"] != "fibsem_liftout_v1":
        raise ReportError("report schema or evaluator identity is invalid")
    if report["openfibsem_commit"] != OPENFIBSEM_COMMIT:
        raise ReportError("OpenFIBSEM provenance is invalid")
    _score(report["score"], "suite score", nullable=bool(report["retry_eligible"]))
    if not isinstance(report["strict_pass"], bool) or not isinstance(
        report["retry_eligible"], bool
    ):
        raise ReportError("report status flags are invalid")
    _boolean_mapping(report["strict_gates"], "strict gates")
    dimensions = report["dimension_scores"]
    if not isinstance(dimensions, Mapping) or set(dimensions) != {
        "step_1",
        "step_2",
        "step_3",
        "step_4",
        "artifacts",
    }:
        raise ReportError("report dimension scores are invalid")
    for name, score in dimensions.items():
        _score(score, f"dimension {name}", maximum=25.0)
    confidence = report["evidence_confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        raise ReportError("evidence confidence is invalid")
    if report["suite"] != {"fixed_worlds": 5, "seeded_worlds": 5}:
        raise ReportError("report suite declaration is invalid")
    worlds = report["worlds"]
    if not isinstance(worlds, list) or len(worlds) != 10:
        raise ReportError("report must contain exactly ten worlds")
    expected_ids = {
        "nominal",
        "small",
        "large",
        "needle_offset",
        "target_pose",
        *(f"seeded_{index:02d}" for index in range(1, 6)),
    }
    actual_ids: set[str] = set()
    for world in worlds:
        if not isinstance(world, Mapping):
            raise ReportError("world report must be an object")
        world_fields = {
            "world_id",
            "category",
            "score",
            "strict_pass",
            "retry_eligible",
            "step_scores",
            "artifact_score",
            "strict_gates",
            "checkpoints",
            "partial_order",
            "terminal",
            "runtime",
            "evidence_confidence",
            "candidate_container_evidence",
            "sim_container_evidence",
            "trusted_evidence",
        }
        if set(world) != world_fields:
            raise ReportError("world fields are invalid")
        world_id = world.get("world_id")
        if not isinstance(world_id, str) or world_id in actual_ids:
            raise ReportError("world report identity is invalid")
        actual_ids.add(world_id)
        expected_category = "fixed" if world_id in {
            "nominal",
            "small",
            "large",
            "needle_offset",
            "target_pose",
        } else "seeded"
        if world.get("category") != expected_category:
            raise ReportError(f"world category is invalid: {world_id}")
        if not isinstance(world.get("strict_pass"), bool) or not isinstance(
            world.get("retry_eligible"), bool
        ):
            raise ReportError(f"world status flags are invalid: {world_id}")
        step_scores = world.get("step_scores")
        if not isinstance(step_scores, Mapping) or set(step_scores) != {
            "step_1",
            "step_2",
            "step_3",
            "step_4",
        }:
            raise ReportError(f"world step scores are invalid: {world_id}")
        for step, maximum in {
            "step_1": 20,
            "step_2": 25,
            "step_3": 25,
            "step_4": 20,
        }.items():
            _score(step_scores[step], f"world {step}: {world_id}", maximum=maximum)
        _score(world.get("artifact_score"), f"artifact score: {world_id}", maximum=10)
        checkpoints = world.get("checkpoints")
        checkpoint_order = ["step_1", "step_2", "step_3", "step_4"]
        if (
            not isinstance(checkpoints, Mapping)
            or list(checkpoints) != checkpoint_order[: len(checkpoints)]
            or world.get("strict_pass")
            and len(checkpoints) != 4
        ):
            raise ReportError(f"checkpoint evidence is incomplete: {world_id}")
        _boolean_mapping(world.get("strict_gates"), f"world strict gates: {world_id}")
        _score(
            world.get("score"),
            f"world score: {world_id}",
            nullable=bool(world.get("retry_eligible")),
        )
        for step, checkpoint in checkpoints.items():
            if not isinstance(checkpoint, Mapping):
                raise ReportError(f"checkpoint evidence is invalid: {world_id}/{step}")
            if checkpoint.get("step_id") != step or not isinstance(
                checkpoint.get("artifact_complete"), bool
            ):
                raise ReportError(f"checkpoint evidence is invalid: {world_id}/{step}")
            digest = checkpoint.get("artifact_digest")
            if checkpoint["artifact_complete"] and not _digest(digest):
                raise ReportError(f"checkpoint artifact digest is invalid: {world_id}/{step}")
            if not isinstance(checkpoint.get("geometry"), Mapping):
                raise ReportError(f"checkpoint geometry is invalid: {world_id}/{step}")
            geometry_hash = checkpoint["geometry"].get("canonical_geometry_hash")
            if not _digest(geometry_hash):
                raise ReportError(f"checkpoint geometry hash is invalid: {world_id}/{step}")
        partial_order = world.get("partial_order")
        if (
            not isinstance(partial_order, Mapping)
            or set(partial_order)
            != {
                "preflight",
                "destructive_roi",
                "step_1",
                "needle_joint",
                "source_separation",
                "carry",
                "step_2",
                "transfer",
                "target_pose",
                "target_joint",
                "step_3",
                "needle_separation",
                "needle_retraction",
                "step_4",
            }
            or any(
                value is not None
                and (isinstance(value, bool) or not isinstance(value, int) or value < 1)
                for value in partial_order.values()
            )
        ):
            raise ReportError(f"world partial-order evidence is invalid: {world_id}")
        terminal = world.get("terminal")
        if (
            not isinstance(terminal, Mapping)
            or set(terminal) != {"safe", "simulator_idle", "collision", "cleanup_error"}
            or any(
                not isinstance(terminal[name], bool)
                for name in ("safe", "simulator_idle", "collision")
            )
            or terminal["cleanup_error"] is not None
            and not isinstance(terminal["cleanup_error"], str)
        ):
            raise ReportError(f"world terminal evidence is invalid: {world_id}")
        runtime = world.get("runtime")
        if not isinstance(runtime, Mapping) or set(runtime) != {
            "candidate_exit_code",
            "timed_out",
            "forbidden_access",
            "infrastructure_failure",
            "candidate_uid",
            "simulator_uid",
            "isolation_verified",
        }:
            raise ReportError(f"world runtime evidence is invalid: {world_id}")
        if runtime["candidate_uid"] != 10001 or runtime["simulator_uid"] != 11001:
            raise ReportError(f"world runtime identities are invalid: {world_id}")
        if any(
            not isinstance(runtime[name], bool)
            for name in (
                "timed_out",
                "forbidden_access",
                "infrastructure_failure",
                "isolation_verified",
            )
        ):
            raise ReportError(f"world runtime flags are invalid: {world_id}")
        exit_code = runtime["candidate_exit_code"]
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ReportError(f"world candidate exit code is invalid: {world_id}")
        candidate_container = world.get("candidate_container_evidence")
        sim_container = world.get("sim_container_evidence")
        trusted = world.get("trusted_evidence")
        if world.get("retry_eligible"):
            if candidate_container is not None:
                _container_evidence(candidate_container, "candidate", world_id)
            if sim_container is not None:
                _container_evidence(sim_container, "sim", world_id)
            if trusted is not None:
                _trusted_evidence(trusted, world_id)
        else:
            _container_evidence(candidate_container, "candidate", world_id)
            _container_evidence(sim_container, "sim", world_id)
            _trusted_evidence(trusted, world_id)
        world_confidence = world.get("evidence_confidence")
        if (
            isinstance(world_confidence, bool)
            or not isinstance(world_confidence, (int, float))
            or not math.isfinite(float(world_confidence))
            or not 0 <= float(world_confidence) <= 1
        ):
            raise ReportError(f"world evidence confidence is invalid: {world_id}")
    if actual_ids != expected_ids:
        raise ReportError("report ten worlds do not match the declared suite")
    if report["strict_pass"] and (
        report["score"] is None
        or float(report["score"]) < 90
        or not all(report["strict_gates"].values())  # type: ignore[union-attr]
    ):
        raise ReportError("strict pass contradicts suite gates")
    return report


def _container_evidence(value: object, role: str, world_id: str) -> None:
    if not isinstance(value, Mapping):
        raise ReportError(f"{role} container evidence is missing: {world_id}")
    required = {
        "container_id",
        "image_digest",
        "network_mode",
        "readonly_rootfs",
        "user",
        "cap_drop",
        "security_options",
        "mounts",
        "cleanup_attempted",
        "cleanup_succeeded",
    }
    if role == "candidate":
        required.add("candidate_status")
    if not required.issubset(value):
        raise ReportError(f"{role} container evidence is incomplete: {world_id}")
    expected_user = "10001:10001" if role == "candidate" else "11001:11001"
    if (
        not _image_digest(value.get("image_digest"))
        or value.get("network_mode") != "none"
        or value.get("readonly_rootfs") is not True
        or value.get("user") != expected_user
        or not isinstance(value.get("cap_drop"), list)
        or "ALL" not in value["cap_drop"]
        or not isinstance(value.get("security_options"), list)
        or "no-new-privileges" not in value["security_options"]
        or value.get("cleanup_attempted") is not True
        or value.get("cleanup_succeeded") is not True
    ):
        raise ReportError(f"{role} container security evidence failed: {world_id}")
    expected_mounts = (
        {"/workspace": False, "/runner": False, "/run/iab": False}
        if role == "candidate"
        else {
            "/run/iab/transport": True,
            "/run/iab/evidence": True,
            "/run/iab/world.json": False,
        }
    )
    mounts = value.get("mounts")
    if not isinstance(mounts, list):
        raise ReportError(f"{role} container mounts are invalid: {world_id}")
    actual: dict[str, bool] = {}
    for mount in mounts:
        if (
            not isinstance(mount, Mapping)
            or mount.get("type") != "bind"
            or not isinstance(mount.get("destination"), str)
            or not isinstance(mount.get("writable"), bool)
            or mount["destination"] in actual
        ):
            raise ReportError(f"{role} container mounts are invalid: {world_id}")
        actual[mount["destination"]] = mount["writable"]
    if actual != expected_mounts:
        raise ReportError(f"{role} container mount boundary failed: {world_id}")


def _trusted_evidence(value: object, world_id: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "journal_head_hash",
        "journal_event_count",
        "outcome",
        "forced_cleanup",
        "scenario_digest",
    }:
        raise ReportError(f"trusted evidence is incomplete: {world_id}")
    count = value["journal_event_count"]
    if (
        not _digest(value["journal_head_hash"])
        or not _digest(value["scenario_digest"])
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        or value["outcome"]
        not in {
            "completed",
            "candidate_incomplete",
            "candidate_failure",
            "infrastructure_failure",
            "cleanup_failure",
        }
        or not isinstance(value["forced_cleanup"], bool)
    ):
        raise ReportError(f"trusted evidence is invalid: {world_id}")


def _image_digest(value: object) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and _digest(
        value.removeprefix("sha256:")
    )


def _boolean_mapping(value: object, name: str) -> None:
    if (
        not isinstance(value, Mapping)
        or not value
        or any(not isinstance(key, str) or not isinstance(item, bool) for key, item in value.items())
    ):
        raise ReportError(f"{name} are invalid")


def _score(
    value: object,
    name: str,
    *,
    nullable: bool = False,
    maximum: float = 100.0,
) -> None:
    if value is None and nullable:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= maximum
    ):
        raise ReportError(f"{name} is invalid")


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
