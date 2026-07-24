from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml

from .errors import ContainerContractError


PROTOCOL_VERSION = 1
PLATFORM = "linux/amd64"
RUNTIME_USER = "10001:10001"


@dataclass(frozen=True)
class ContainerLimits:
    cpus: float
    memory_mb: int
    pids: int
    timeout_seconds: float
    stdout_bytes: int
    stderr_bytes: int


@dataclass(frozen=True)
class EvaluatorMaxima:
    cpus: float = 1.0
    memory_mb: int = 512
    pids: int = 64
    timeout_seconds: float = 30.0
    stdout_bytes: int = 1_048_576
    stderr_bytes: int = 1_048_576


@dataclass(frozen=True)
class EffectiveContainerPolicy:
    cpus: float
    memory_mb: int
    pids: int
    timeout_seconds: float
    stdout_bytes: int
    stderr_bytes: int


@dataclass(frozen=True)
class ImageLock:
    schema_version: int
    container_protocol_version: int
    platform: str
    base_image: str
    dockerfile_sha256: str
    image_reference: str
    image_digest: str
    runtime_user: str


@dataclass(frozen=True)
class ContainerContract:
    instance_root: Path
    protocol_version: int
    dockerfile: Path
    lock_file: Path
    context_files: Mapping[str, str]
    platform: str
    user: str
    workdir: str
    entrypoint: tuple[str, ...]
    gateway_path: str
    output_path: str
    limits: ContainerLimits
    lock: ImageLock


def load_container_contract(instance_root: Path) -> ContainerContract:
    root = instance_root.resolve()
    instance = _load_yaml(root / "instance.yaml", "instance manifest")
    value = instance.get("container")
    if not isinstance(value, dict):
        raise ContainerContractError("container contract is missing")
    _exact_keys(
        value,
        {
            "protocol_version",
            "dockerfile",
            "lock_file",
            "context_files",
            "platform",
            "user",
            "workdir",
            "entrypoint",
            "gateway_path",
            "output_path",
            "limits",
        },
        "container",
    )
    protocol = _exact(value["protocol_version"], PROTOCOL_VERSION, "protocol")
    platform = _exact(value["platform"], PLATFORM, "platform")
    user = _exact(value["user"], RUNTIME_USER, "user")
    dockerfile = _child_file(root, value["dockerfile"], "dockerfile")
    lock_file = _child_file(root, value["lock_file"], "lock file")
    context_files = value["context_files"]
    if not isinstance(context_files, dict) or not context_files:
        raise ContainerContractError("context_files must be a non-empty mapping")
    expected_context = {
        dockerfile.relative_to(root).as_posix(),
        lock_file.relative_to(root).as_posix(),
    }
    if set(context_files) != expected_context:
        raise ContainerContractError("context_files must contain Dockerfile and lock")
    for relative, expected_hash in context_files.items():
        path = _child_file(root, relative, "context file")
        _verify_hash(path, expected_hash)
    workdir = _absolute_container_path(value["workdir"], "workdir")
    gateway = _absolute_container_path(value["gateway_path"], "gateway_path")
    output = _absolute_container_path(value["output_path"], "output_path")
    entrypoint = value["entrypoint"]
    if (
        not isinstance(entrypoint, list)
        or len(entrypoint) < 2
        or not all(isinstance(item, str) and item for item in entrypoint)
    ):
        raise ContainerContractError("entrypoint must contain command arguments")
    limits = _load_limits(value["limits"])
    lock = _load_image_lock(lock_file)
    if lock.container_protocol_version != protocol:
        raise ContainerContractError("lock protocol does not match container protocol")
    if lock.platform != platform:
        raise ContainerContractError("lock platform does not match container platform")
    if lock.runtime_user != user:
        raise ContainerContractError("lock runtime user does not match container user")
    actual_dockerfile_hash = hashlib.sha256(dockerfile.read_bytes()).hexdigest()
    if lock.dockerfile_sha256 != actual_dockerfile_hash:
        raise ContainerContractError("Dockerfile hash does not match image lock")
    return ContainerContract(
        instance_root=root,
        protocol_version=protocol,
        dockerfile=dockerfile,
        lock_file=lock_file,
        context_files=dict(context_files),
        platform=platform,
        user=user,
        workdir=workdir,
        entrypoint=tuple(entrypoint),
        gateway_path=gateway,
        output_path=output,
        limits=limits,
        lock=lock,
    )


def effective_policy(
    contract: ContainerContract,
    maxima: EvaluatorMaxima = EvaluatorMaxima(),
) -> EffectiveContainerPolicy:
    return EffectiveContainerPolicy(
        cpus=min(contract.limits.cpus, maxima.cpus),
        memory_mb=min(contract.limits.memory_mb, maxima.memory_mb),
        pids=min(contract.limits.pids, maxima.pids),
        timeout_seconds=min(
            contract.limits.timeout_seconds, maxima.timeout_seconds
        ),
        stdout_bytes=min(contract.limits.stdout_bytes, maxima.stdout_bytes),
        stderr_bytes=min(contract.limits.stderr_bytes, maxima.stderr_bytes),
    )


def _load_limits(raw: Any) -> ContainerLimits:
    if not isinstance(raw, dict):
        raise ContainerContractError("limits must be a mapping")
    names = {
        "cpus",
        "memory_mb",
        "pids",
        "timeout_seconds",
        "stdout_bytes",
        "stderr_bytes",
    }
    _exact_keys(raw, names, "limits")
    return ContainerLimits(
        cpus=_positive_number(raw["cpus"], "cpus"),
        memory_mb=_positive_int(raw["memory_mb"], "memory_mb"),
        pids=_positive_int(raw["pids"], "pids"),
        timeout_seconds=_positive_number(
            raw["timeout_seconds"], "timeout_seconds"
        ),
        stdout_bytes=_positive_int(raw["stdout_bytes"], "stdout_bytes"),
        stderr_bytes=_positive_int(raw["stderr_bytes"], "stderr_bytes"),
    )


def _load_image_lock(path: Path) -> ImageLock:
    raw = _load_yaml(path, "image lock")
    _exact_keys(
        raw,
        {
            "schema_version",
            "container_protocol_version",
            "platform",
            "base_image",
            "dockerfile_sha256",
            "built_image",
            "runtime_user",
        },
        "image lock",
    )
    built = raw["built_image"]
    if not isinstance(built, dict):
        raise ContainerContractError("built_image must be a mapping")
    _exact_keys(built, {"reference", "digest"}, "built_image")
    base = _non_empty(raw["base_image"], "base_image")
    digest = _digest(built["digest"], "built image digest")
    if "@sha256:" not in base:
        raise ContainerContractError("base_image must be digest-pinned")
    _digest(base.rsplit("@", 1)[1], "base image digest")
    return ImageLock(
        schema_version=_exact(raw["schema_version"], 1, "lock schema"),
        container_protocol_version=_positive_int(
            raw["container_protocol_version"], "container protocol"
        ),
        platform=_non_empty(raw["platform"], "lock platform"),
        base_image=base,
        dockerfile_sha256=_hex_hash(
            raw["dockerfile_sha256"], "dockerfile_sha256"
        ),
        image_reference=_non_empty(built["reference"], "image reference"),
        image_digest=digest,
        runtime_user=_non_empty(raw["runtime_user"], "runtime user"),
    )


def _load_yaml(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ContainerContractError(f"cannot load {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContainerContractError(f"{label} must contain a mapping")
    return value


def _child_file(root: Path, raw: Any, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ContainerContractError(f"{label} path must be a string")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ContainerContractError(f"{label} path escapes instance")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ContainerContractError(f"{label} path is not a file")
    return path


def _absolute_container_path(raw: Any, name: str) -> str:
    if not isinstance(raw, str) or not PurePosixPath(raw).is_absolute():
        raise ContainerContractError(f"{name} must be an absolute container path")
    return raw


def _verify_hash(path: Path, expected: Any) -> None:
    expected_hash = _hex_hash(expected, f"{path.name} hash")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_hash:
        raise ContainerContractError(f"context file hash mismatch: {path.name}")


def _digest(raw: Any, name: str) -> str:
    value = _non_empty(raw, name)
    if not value.startswith("sha256:"):
        raise ContainerContractError(f"{name} must use sha256")
    _hex_hash(value.removeprefix("sha256:"), name)
    return value


def _hex_hash(raw: Any, name: str) -> str:
    value = _non_empty(raw, name)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ContainerContractError(f"{name} must be 64 lowercase hex characters")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ContainerContractError(f"{label} fields do not match protocol")


def _exact(raw: Any, expected: Any, name: str):
    if raw != expected:
        raise ContainerContractError(f"unsupported {name}")
    return raw


def _non_empty(raw: Any, name: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ContainerContractError(f"{name} must be a non-empty string")
    return raw


def _positive_number(raw: Any, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise ContainerContractError(f"{name} must be positive")
    return float(raw)


def _positive_int(raw: Any, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ContainerContractError(f"{name} must be a positive integer")
    return raw
