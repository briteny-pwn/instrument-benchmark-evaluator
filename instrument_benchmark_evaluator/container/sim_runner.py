from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from sources.pyvisa.pyvisa_dut_validation_v2.broker import OPERATIONS
from sources.pyvisa.pyvisa_dut_validation_v2.protocol import RpcClient

from .docker_client import DockerClient
from .errors import ContainerInfrastructureError
from .evidence import ContainerEvidence, normalize_inspect
from .sim_evidence import SimJournalEvidence, verify_evidence


ReadinessProbe = Callable[[Path, float], bool]
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class SimContainerHandle:
    container_id: str
    name: str
    run_id: str
    world_id: str
    endpoint: Path
    evidence_dir: Path
    world_path: Path


@dataclass(frozen=True)
class SimContainerResult:
    container_evidence: ContainerEvidence
    journal_evidence: SimJournalEvidence
    fatal: dict | None
    stdout_sha256: str = EMPTY_SHA256
    stderr_sha256: str = EMPTY_SHA256


class SimContainerRunner:
    def __init__(
        self,
        *,
        client: DockerClient,
        evaluator_image_id: str,
        readiness_probe: ReadinessProbe | None = None,
        readiness_timeout: float = 10.0,
        stop_timeout: float = 5.0,
    ) -> None:
        if not IMAGE_ID.fullmatch(evaluator_image_id):
            raise ValueError("evaluator image ID must be exact")
        self.client = client
        self.evaluator_image_id = evaluator_image_id
        self.readiness_probe = readiness_probe or _probe_readiness
        self.readiness_timeout = readiness_timeout
        self.stop_timeout = stop_timeout

    def start(
        self,
        *,
        run_id: str,
        world_id: str,
        world_path: Path,
        transport_dir: Path,
        evidence_dir: Path,
    ) -> SimContainerHandle:
        if world_path.is_symlink() or not world_path.is_file():
            raise ContainerInfrastructureError("hidden world file is invalid")
        world_path = world_path.resolve()
        _prepare_empty_directory(transport_dir, "transport")
        _prepare_empty_directory(evidence_dir, "evidence")
        name = _name(run_id, world_id)
        endpoint = transport_dir / "visa.sock"
        arguments = _create_arguments(
            image_id=self.evaluator_image_id,
            name=name,
            run_id=run_id,
            world_id=world_id,
            world_path=world_path,
            transport_dir=transport_dir.resolve(),
            evidence_dir=evidence_dir.resolve(),
        )
        container_id: str | None = None
        try:
            created = self.client.run(arguments)
            container_id = created.stdout.strip()
            if not container_id or "\n" in container_id:
                raise ContainerInfrastructureError(
                    "docker create did not return sim container ID"
                )
            self.client.start_detached(container_id)
            if not self.readiness_probe(endpoint, self.readiness_timeout):
                value = self.client.inspect(container_id)
                state = value.get("State")
                if isinstance(state, dict) and state.get("Status") == "exited":
                    raise ContainerInfrastructureError(
                        "sim exited before readiness with code "
                        f"{state.get('ExitCode', 'unknown')}"
                    )
                raise ContainerInfrastructureError("sim readiness timed out")
            return SimContainerHandle(
                container_id=container_id,
                name=name,
                run_id=run_id,
                world_id=world_id,
                endpoint=endpoint,
                evidence_dir=evidence_dir.resolve(),
                world_path=world_path,
            )
        except BaseException as primary_error:
            if container_id is not None:
                try:
                    self.client.remove(container_id)
                except ContainerInfrastructureError as cleanup_error:
                    raise cleanup_error from primary_error
            raise

    def finalize(self, handle: SimContainerHandle) -> SimContainerResult:
        evidence: ContainerEvidence | None = None
        journal: SimJournalEvidence | None = None
        primary_error: BaseException | None = None
        cleanup_error: ContainerInfrastructureError | None = None
        exit_code: int | None = None
        signal_error: ContainerInfrastructureError | None = None
        try:
            try:
                self.client.signal(handle.container_id, "TERM")
            except ContainerInfrastructureError as exc:
                signal_error = exc
                primary_error = exc
            if signal_error is None:
                try:
                    exit_code = self.client.wait(
                        handle.container_id, self.stop_timeout
                    )
                except BaseException as exc:
                    primary_error = exc
            try:
                evidence = normalize_inspect(
                    self.client.inspect(handle.container_id)
                )
                _validate_runtime(evidence, self.evaluator_image_id, handle)
                if signal_error is not None and evidence.status == "exited":
                    primary_error = None
                    exit_code = evidence.exit_code
                elif primary_error is None and (
                    evidence.status != "exited" or evidence.exit_code != exit_code
                ):
                    primary_error = ContainerInfrastructureError(
                        "sim wait result does not match inspect state"
                    )
            except BaseException as exc:
                if primary_error is None:
                    primary_error = exc
            try:
                journal = verify_evidence(
                    handle.evidence_dir,
                    run_id=handle.run_id,
                    world_id=handle.world_id,
                )
            except BaseException as exc:
                if primary_error is None:
                    primary_error = exc
            if evidence is not None and evidence.oom_killed and primary_error is None:
                primary_error = ContainerInfrastructureError(
                    "sim container was OOM killed"
                )
            if (
                primary_error is None
                and exit_code != 0
                and not (
                    exit_code == 70
                    and journal is not None
                    and journal.fatal is not None
                )
            ):
                primary_error = ContainerInfrastructureError(
                    f"sim container exited with code {exit_code}"
                )
        finally:
            try:
                self.client.remove(handle.container_id)
            except ContainerInfrastructureError as exc:
                cleanup_error = exc
            if evidence is not None:
                evidence = replace(
                    evidence,
                    cleanup_attempted=True,
                    cleanup_succeeded=cleanup_error is None,
                    cleanup_error=None if cleanup_error is None else str(cleanup_error),
                )
        if cleanup_error is not None:
            raise cleanup_error
        if primary_error is not None:
            raise primary_error
        assert evidence is not None and journal is not None
        return SimContainerResult(evidence, journal, journal.fatal)


def _create_arguments(
    *,
    image_id: str,
    name: str,
    run_id: str,
    world_id: str,
    world_path: Path,
    transport_dir: Path,
    evidence_dir: Path,
) -> list[str]:
    return [
        "create",
        f"--name={name}",
        "--label=iab.managed=true",
        "--label=iab.role=sim",
        f"--label=iab.owner={os.environ.get('IAB_CONTAINER_OWNER', run_id)}",
        f"--label=iab.run={run_id}",
        f"--label=iab.world={world_id}",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--log-driver=none",
        "--user=11001:11001",
        "--cpus=1.0",
        "--memory=512m",
        "--memory-swap=512m",
        "--pids-limit=64",
        "--ulimit=nofile=256:256",
        "--stop-timeout=2",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,uid=11001,gid=11001",
        f"--mount=type=bind,src={transport_dir},dst=/run/iab/transport",
        f"--mount=type=bind,src={evidence_dir},dst=/run/iab/evidence",
        f"--mount=type=bind,src={world_path},dst=/run/iab/world.json,readonly",
        image_id,
        "serve-sim",
        "--world",
        "/run/iab/world.json",
        "--endpoint",
        "/run/iab/transport/visa.sock",
        "--evidence",
        "/run/iab/evidence",
        "--run-id",
        run_id,
    ]


def _validate_runtime(
    evidence: ContainerEvidence,
    image_id: str,
    handle: SimContainerHandle,
) -> None:
    mounts = {mount.destination: mount for mount in evidence.mounts}
    checks = {
        "image": evidence.image_digest == image_id,
        "user": evidence.user == "11001:11001",
        "network": evidence.network_mode == "none",
        "rootfs": evidence.readonly_rootfs,
        "capabilities": "ALL" in evidence.cap_drop,
        "privileges": "no-new-privileges" in evidence.security_options,
        "memory": evidence.memory_bytes == 512 * 1024 * 1024,
        "swap": evidence.memory_swap_bytes == 512 * 1024 * 1024,
        "cpu": evidence.nano_cpus == 1_000_000_000,
        "pids": evidence.pids_limit == 64,
        "logs": evidence.log_driver == "none",
        "nofile": evidence.ulimits == ("nofile:256:256",),
        "stop timeout": evidence.stop_timeout == 2,
        "tmpfs": evidence.tmpfs
        == ("/tmp:rw,noexec,nosuid,nodev,size=64m,uid=11001,gid=11001",),
        "pid namespace": evidence.pid_mode == "",
        "ipc namespace": evidence.ipc_mode in {"", "private"},
        "uts namespace": evidence.uts_mode == "",
        "mount allowlist": set(mounts)
        == {"/run/iab/transport", "/run/iab/evidence", "/run/iab/world.json"},
        "transport writable": mounts.get("/run/iab/transport") is not None
        and mounts["/run/iab/transport"].mount_type == "bind"
        and mounts["/run/iab/transport"].writable
        and Path(mounts["/run/iab/transport"].source).resolve()
        == handle.endpoint.parent.resolve(),
        "evidence writable": mounts.get("/run/iab/evidence") is not None
        and mounts["/run/iab/evidence"].mount_type == "bind"
        and mounts["/run/iab/evidence"].writable
        and Path(mounts["/run/iab/evidence"].source).resolve()
        == handle.evidence_dir.resolve(),
        "world read-only": mounts.get("/run/iab/world.json") is not None
        and mounts["/run/iab/world.json"].mount_type == "bind"
        and not mounts["/run/iab/world.json"].writable,
        "world source": mounts.get("/run/iab/world.json") is not None
        and Path(mounts["/run/iab/world.json"].source).resolve()
        == handle.world_path.resolve(),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ContainerInfrastructureError(
            "sim runtime policy mismatch: " + ", ".join(failed)
        )


def _probe_readiness(endpoint: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client: RpcClient | None = None
        try:
            remaining = max(0.001, deadline - time.monotonic())
            client = RpcClient(
                str(endpoint), socket_timeout=min(remaining, 0.25)
            )
            result, status = client.call("hello", {})
            return status is None and result == tuple(sorted(OPERATIONS))
        except (OSError, ValueError):
            time.sleep(0.02)
        finally:
            if client is not None:
                client.close()
    return False


def _name(run_id: str, world_id: str) -> str:
    raw = f"iab-sim-{run_id}-{world_id}-{secrets.token_hex(6)}"
    return "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in raw
    )[:128]


def _prepare_empty_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ContainerInfrastructureError(f"sim {label} directory is invalid")
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise ContainerInfrastructureError(
                f"sim {label} directory must be empty"
            )
        return
    path.mkdir(parents=True, exist_ok=False)
