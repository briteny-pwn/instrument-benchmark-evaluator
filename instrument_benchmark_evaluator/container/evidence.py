from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ContainerInfrastructureError


@dataclass(frozen=True)
class MountEvidence:
    mount_type: str
    source: str
    destination: str
    mode: str
    writable: bool


@dataclass(frozen=True)
class ContainerEvidence:
    container_id: str
    image_digest: str
    created_at: str
    started_at: str
    finished_at: str
    status: str
    exit_code: int
    oom_killed: bool
    user: str
    network_mode: str
    readonly_rootfs: bool
    cap_drop: tuple[str, ...]
    security_options: tuple[str, ...]
    memory_bytes: int
    nano_cpus: int
    pids_limit: int
    pid_mode: str
    ipc_mode: str
    uts_mode: str
    mounts: tuple[MountEvidence, ...]
    cleanup_attempted: bool = False
    cleanup_succeeded: bool | None = None
    cleanup_error: str | None = None


def normalize_inspect(value: dict[str, Any]) -> ContainerEvidence:
    config = _mapping(value, "Config")
    host = _mapping(value, "HostConfig")
    state = _mapping(value, "State")
    raw_mounts = value.get("Mounts", [])
    if not isinstance(raw_mounts, list):
        raise ContainerInfrastructureError("inspect Mounts must be a list")
    mounts = tuple(_mount(item) for item in raw_mounts)
    return ContainerEvidence(
        container_id=_text(value, "Id"),
        image_digest=_text(value, "Image"),
        created_at=_text(value, "Created"),
        started_at=_text(state, "StartedAt"),
        finished_at=_text(state, "FinishedAt"),
        status=_text(state, "Status"),
        exit_code=_integer(state, "ExitCode"),
        oom_killed=_boolean(state, "OOMKilled"),
        user=_text(config, "User"),
        network_mode=_text(host, "NetworkMode"),
        readonly_rootfs=_boolean(host, "ReadonlyRootfs"),
        cap_drop=_strings(host.get("CapDrop", []), "CapDrop"),
        security_options=_strings(host.get("SecurityOpt", []), "SecurityOpt"),
        memory_bytes=_integer(host, "Memory"),
        nano_cpus=_integer(host, "NanoCpus"),
        pids_limit=_integer(host, "PidsLimit"),
        pid_mode=_optional_text(host.get("PidMode")),
        ipc_mode=_optional_text(host.get("IpcMode")),
        uts_mode=_optional_text(host.get("UTSMode")),
        mounts=mounts,
    )


def _mount(value: Any) -> MountEvidence:
    if not isinstance(value, dict):
        raise ContainerInfrastructureError("inspect mount must be an object")
    return MountEvidence(
        mount_type=_text(value, "Type"),
        source=_text(value, "Source"),
        destination=_text(value, "Destination"),
        mode=_optional_text(value.get("Mode")),
        writable=_boolean(value, "RW"),
    )


def _mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ContainerInfrastructureError(f"inspect {key} must be an object")
    return result


def _text(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise ContainerInfrastructureError(f"inspect {key} must be a string")
    return result


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ContainerInfrastructureError("inspect namespace mode must be a string")
    return value


def _integer(value: dict[str, Any], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ContainerInfrastructureError(f"inspect {key} must be an integer")
    return result


def _boolean(value: dict[str, Any], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ContainerInfrastructureError(f"inspect {key} must be a boolean")
    return result


def _strings(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ContainerInfrastructureError(f"inspect {key} must be string list")
    return tuple(value)
