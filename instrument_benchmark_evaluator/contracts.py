from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import EVALUATOR_ID, PROTOCOL_VERSION
from .container.contracts import ContainerContract, load_container_contract
from .container.errors import ContainerContractError


class ContractError(ValueError):
    """A distributed evaluator request or instance manifest is invalid."""


@dataclass(frozen=True)
class EvaluatorRequest:
    protocol_version: int
    run_id: str
    instance_id: str
    instance_path: Path
    candidate_path: Path
    timeout_seconds: float
    max_output_bytes: int
    repeated_worlds: int
    repeated_base_seed: int
    container_protocol_version: int
    image_mode: str
    shared_run_root: Path
    evaluator_image_id: str | None = None


@dataclass(frozen=True)
class InstanceSettings:
    instance_id: str
    visible_files: tuple[str, ...]
    submission_filename: str
    result_filename: str
    forbidden_import_roots: tuple[str, ...]
    container: ContainerContract


@dataclass(frozen=True)
class RunSettings:
    instance_path: Path
    fixed_worlds: tuple[str, ...]
    repeated_worlds: int
    timeout_seconds: float
    max_output_bytes: int
    run_id: str = "run"
    shared_run_root: Path | None = None


def evaluator_kind(instance_id: str) -> str:
    kinds = {
        "pyvisa_dut_validation_v1": "pyvisa_v1",
        "pyvisa_dut_validation_v2": "pyvisa_v2",
        "fibsem_liftout_v1": "fibsem",
    }
    try:
        return kinds[instance_id]
    except KeyError as exc:
        raise ContractError("unsupported instance_id") from exc


def load_evaluator_request(path: Path) -> EvaluatorRequest:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load request: {exc}") from exc
    required = {
        "protocol_version",
        "run_id",
        "instance_id",
        "instance_path",
        "candidate_path",
        "timeout_seconds",
        "max_output_bytes",
        "repeated_worlds",
        "repeated_base_seed",
        "container_protocol_version",
        "image_mode",
        "shared_run_root",
    }
    if not isinstance(value, dict):
        raise ContractError("request fields do not match protocol version 1")
    instance_id = value.get("instance_id")
    kind = evaluator_kind(instance_id) if isinstance(instance_id, str) else None
    if kind == "pyvisa_v1":
        expected_fields = required
    elif kind in {"pyvisa_v2", "fibsem"}:
        expected_fields = required | {"evaluator_image_id"}
    else:
        raise ContractError("unsupported instance_id")
    if set(value) != expected_fields:
        raise ContractError("request fields do not match evaluator protocol")
    if value["protocol_version"] != PROTOCOL_VERSION:
        raise ContractError("unsupported protocol_version")
    if value["container_protocol_version"] != 1:
        raise ContractError("unsupported container_protocol_version")
    if value["image_mode"] != "locked":
        raise ContractError("image_mode must be locked")
    instance_path = _absolute_existing_directory(value["instance_path"], "instance_path")
    candidate_path = _absolute_existing_file(value["candidate_path"], "candidate_path")
    timeout = _positive_number(value["timeout_seconds"], "timeout_seconds")
    output_limit = _positive_int(value["max_output_bytes"], "max_output_bytes")
    repeated = _positive_int(value["repeated_worlds"], "repeated_worlds")
    seed = _positive_int(value["repeated_base_seed"], "repeated_base_seed")
    shared_run_root = _absolute_existing_directory(
        value["shared_run_root"], "shared_run_root"
    )
    if shared_run_root == Path(shared_run_root.anchor):
        raise ContractError("shared_run_root must not be a filesystem root")
    run_id = value["run_id"]
    if not isinstance(run_id, str) or not run_id:
        raise ContractError("run_id must be a non-empty string")
    evaluator_image_id = (
        _sha256_digest(value["evaluator_image_id"], "evaluator_image_id")
        if kind in {"pyvisa_v2", "fibsem"}
        else None
    )
    return EvaluatorRequest(
        protocol_version=PROTOCOL_VERSION,
        run_id=run_id,
        instance_id=instance_id,
        instance_path=instance_path,
        candidate_path=candidate_path,
        timeout_seconds=timeout,
        max_output_bytes=output_limit,
        repeated_worlds=repeated,
        repeated_base_seed=seed,
        container_protocol_version=1,
        image_mode="locked",
        shared_run_root=shared_run_root,
        evaluator_image_id=evaluator_image_id,
    )


def load_instance_settings(
    instance_path: Path,
    expected_evaluator_id: str = EVALUATOR_ID,
) -> InstanceSettings:
    manifest_path = instance_path / "instance.yaml"
    try:
        value = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError(f"cannot load instance manifest: {exc}") from exc
    if (
        expected_evaluator_id
        not in {
            "pyvisa_dut_validation_v1",
            "pyvisa_dut_validation_v2",
            "fibsem_liftout_v1",
        }
        or not isinstance(value, dict)
        or value.get("instance_id") != expected_evaluator_id
    ):
        raise ContractError("instance manifest has unsupported instance_id")
    evaluator = value.get("evaluator")
    if evaluator != {
        "id": expected_evaluator_id,
        "protocol_version": PROTOCOL_VERSION,
    }:
        raise ContractError("instance evaluator contract is incompatible")
    visible = value.get("visible_files")
    if not isinstance(visible, dict) or not visible:
        raise ContractError("instance visible_files must be a non-empty mapping")
    submission = value.get("submission")
    runtime = value.get("runtime")
    if not isinstance(submission, dict) or not isinstance(runtime, dict):
        raise ContractError("instance submission/runtime is invalid")
    try:
        container = load_container_contract(instance_path)
    except ContainerContractError as exc:
        raise ContractError(f"invalid instance container contract: {exc}") from exc
    return InstanceSettings(
        instance_id=expected_evaluator_id,
        visible_files=tuple(visible),
        submission_filename=str(submission["filename"]),
        result_filename=str(submission["result_filename"]),
        forbidden_import_roots=tuple(runtime["forbidden_import_roots"]),
        container=container,
    )


def _absolute_existing_directory(raw: Any, name: str) -> Path:
    path = _absolute_path(raw, name)
    if not path.is_dir():
        raise ContractError(f"{name} must be an existing directory")
    return path


def _absolute_existing_file(raw: Any, name: str) -> Path:
    path = _absolute_path(raw, name)
    if not path.is_file():
        raise ContractError(f"{name} must be an existing file")
    return path


def _absolute_path(raw: Any, name: str) -> Path:
    if not isinstance(raw, str):
        raise ContractError(f"{name} must be a string")
    path = Path(raw)
    if not path.is_absolute():
        raise ContractError(f"{name} must be absolute")
    return path.resolve()


def _positive_number(raw: Any, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise ContractError(f"{name} must be positive")
    return float(raw)


def _positive_int(raw: Any, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ContractError(f"{name} must be a positive integer")
    return raw


def _sha256_digest(raw: Any, name: str) -> str:
    if not isinstance(raw, str) or not raw.startswith("sha256:"):
        raise ContractError(f"{name} must be a sha256 image ID")
    digest = raw.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ContractError(f"{name} must contain 64 lowercase hex characters")
    return raw
