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

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.mount_type,
            "source": self.source,
            "destination": self.destination,
            "mode": self.mode,
            "writable": self.writable,
        }


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
    memory_swap_bytes: int = 0
    log_driver: str = ""
    ulimits: tuple[str, ...] = ()
    stop_timeout: int = 0
    tmpfs: tuple[str, ...] = ()
    cleanup_attempted: bool = False
    cleanup_succeeded: bool | None = None
    cleanup_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "container_id": self.container_id,
            "image_digest": self.image_digest,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "oom_killed": self.oom_killed,
            "user": self.user,
            "network_mode": self.network_mode,
            "readonly_rootfs": self.readonly_rootfs,
            "cap_drop": list(self.cap_drop),
            "security_options": list(self.security_options),
            "memory_bytes": self.memory_bytes,
            "nano_cpus": self.nano_cpus,
            "pids_limit": self.pids_limit,
            "memory_swap_bytes": self.memory_swap_bytes,
            "log_driver": self.log_driver,
            "ulimits": list(self.ulimits),
            "stop_timeout": self.stop_timeout,
            "tmpfs": list(self.tmpfs),
            "pid_mode": self.pid_mode,
            "ipc_mode": self.ipc_mode,
            "uts_mode": self.uts_mode,
            "mounts": [mount.to_dict() for mount in self.mounts],
            "cleanup_attempted": self.cleanup_attempted,
            "cleanup_succeeded": self.cleanup_succeeded,
            "cleanup_error": self.cleanup_error,
        }


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
        memory_swap_bytes=_integer(host, "MemorySwap"),
        log_driver=_text(_mapping(host, "LogConfig"), "Type"),
        ulimits=_ulimits(host.get("Ulimits", [])),
        stop_timeout=_integer(config, "StopTimeout"),
        tmpfs=_tmpfs(host.get("Tmpfs", {})),
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


def _ulimits(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContainerInfrastructureError("inspect Ulimits must be a list")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ContainerInfrastructureError("inspect Ulimit must be an object")
        result.append(
            f"{_text(item, 'Name')}:{_integer(item, 'Soft')}:{_integer(item, 'Hard')}"
        )
    return tuple(result)


def _tmpfs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(options, str)
        for key, options in value.items()
    ):
        raise ContainerInfrastructureError("inspect Tmpfs must be a string mapping")
    return tuple(f"{key}:{value[key]}" for key in sorted(value))
