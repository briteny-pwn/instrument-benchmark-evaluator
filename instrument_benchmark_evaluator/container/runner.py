from __future__ import annotations

import secrets
import os
import time
import base64
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .contracts import ContainerContract, EffectiveContainerPolicy
from .docker_client import DockerClient
from .errors import ContainerInfrastructureError
from .evidence import ContainerEvidence, normalize_inspect
from .output import ArtifactEvidence, OutputCollectionError


@dataclass(frozen=True)
class ContainerProcessResult:
    status: str
    returncode: int | None
    stdout: str
    stderr: str
    result: dict[str, Any] | None
    container_evidence: ContainerEvidence
    artifact_evidence: ArtifactEvidence | None
    candidate_status: str | None = None


def run_container(
    *,
    contract: ContainerContract,
    policy: EffectiveContainerPolicy,
    image_digest: str,
    workspace: Path,
    output_dir: Path,
    gateway_socket: Path,
    runner_dir: Path,
    client: DockerClient,
    run_id: str,
    world_id: str,
    expected_output_uid: int | None = None,
) -> ContainerProcessResult:
    name = _container_name(run_id, world_id)
    container_id: str | None = None
    evidence: ContainerEvidence | None = None
    cleanup_error: str | None = None
    status = "infrastructure_failure"
    returncode: int | None = None
    stdout = ""
    stderr = ""
    result: dict[str, Any] | None = None
    artifact: ArtifactEvidence | None = None
    live_result: dict[str, Any] | None = None
    live_artifact: ArtifactEvidence | None = None
    live_error: str | None = None
    candidate_status: str | None = None
    try:
        created = client.run(
            _create_arguments(
                contract=contract,
                policy=policy,
                image_digest=image_digest,
                workspace=workspace,
                output_dir=output_dir,
                gateway_socket=gateway_socket,
                runner_dir=runner_dir,
                name=name,
                run_id=run_id,
                world_id=world_id,
            )
        )
        container_id = created.stdout.strip()
        if not container_id or "\n" in container_id:
            raise ContainerInfrastructureError(
                "docker create did not return one container ID"
            )
        def copy_artifacts() -> None:
            nonlocal live_result, live_artifact, live_error
            try:
                live_result, live_artifact = _collect_live_artifacts(
                    client, container_id, policy.stdout_bytes
                )
            except OutputCollectionError as exc:
                live_error = str(exc)

        attached = client.start_attached(
            container_id,
            timeout=policy.timeout_seconds,
            stdout_limit=policy.stdout_bytes,
            stderr_limit=policy.stderr_bytes,
            artifact_callback=copy_artifacts,
        )
        returncode = attached.returncode
        stdout = attached.stdout
        stderr = attached.stderr
        evidence = normalize_inspect(client.inspect(container_id))
        _validate_runtime(evidence, contract, policy, image_digest)
        if attached.completed_signal:
            if live_error is not None or live_result is None:
                status = "invalid_result"
                stderr = (
                    f"{stderr}\ninvalid result: {live_error or 'missing artifact'}"
                ).strip()
            else:
                status = "completed"
                result = live_result
                artifact = live_artifact
        elif attached.timed_out:
            status = "candidate_timeout"
        elif attached.output_limited:
            status = "output_limit"
        elif evidence.oom_killed:
            status = "candidate_oom"
        elif returncode == 0:
            status = "invalid_result"
            stderr = f"{stderr}\nmissing trusted bootstrap completion signal".strip()
        elif returncode in {2, 3}:
            status = "invalid_result"
        else:
            status = "candidate_failure"
    finally:
        candidate_status = status
        if container_id is not None:
            try:
                client.remove(container_id)
            except ContainerInfrastructureError as exc:
                cleanup_error = str(exc)
            if evidence is not None:
                evidence = replace(
                    evidence,
                    cleanup_attempted=True,
                    cleanup_succeeded=cleanup_error is None,
                    cleanup_error=cleanup_error,
                )
        if cleanup_error is not None:
            status = "infrastructure_failure"
    if evidence is None:
        raise ContainerInfrastructureError(
            "container evidence unavailable after lifecycle failure"
        )
    return ContainerProcessResult(
        status=status,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        result=result,
        container_evidence=evidence,
        artifact_evidence=artifact,
        candidate_status=candidate_status,
    )


def _create_arguments(
    *,
    contract: ContainerContract,
    policy: EffectiveContainerPolicy,
    image_digest: str,
    workspace: Path,
    output_dir: Path,
    gateway_socket: Path,
    runner_dir: Path,
    name: str,
    run_id: str,
    world_id: str,
) -> list[str]:
    solution = f"{contract.workdir}/solution.py"
    returned = str(Path(contract.output_path).with_name("return.json"))
    return [
        "create",
        f"--name={name}",
        "--label=iab.managed=true",
        f"--label=iab.run={run_id}",
        f"--label=iab.world={world_id}",
        f"--label=iab.evaluator={contract.instance_root.name}",
        f"--label=iab.owner={os.environ.get('IAB_CONTAINER_OWNER', run_id)}",
        f"--label=iab.expires={int(time.time()) + 3600}",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--log-driver=none",
        f"--user={contract.user}",
        "--env=IAB_CONTAINER_MODE=1",
        f"--workdir={contract.workdir}",
        f"--cpus={policy.cpus}",
        f"--memory={policy.memory_mb}m",
        f"--memory-swap={policy.memory_mb}m",
        f"--pids-limit={policy.pids}",
        "--ulimit=nofile=256:256",
        "--stop-timeout=1",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
        f"--mount=type=bind,src={workspace.resolve()},dst={contract.workdir},readonly",
        f"--mount=type=bind,src={runner_dir.resolve()},dst=/runner,readonly",
        (
            "--mount=type=bind,"
            f"src={gateway_socket.parent.resolve()},dst=/run/iab,readonly"
        ),
        (
            "--tmpfs=/output:rw,nosuid,nodev,noexec,"
            "uid=10001,gid=10001,mode=0770,"
            f"size={max(policy.stdout_bytes, policy.stderr_bytes) * 4}"
        ),
        image_digest,
        solution,
        contract.gateway_path,
        contract.output_path,
        returned,
    ]


def _container_name(run_id: str, world_id: str) -> str:
    raw = f"iab-{run_id}-{world_id}-{secrets.token_hex(6)}"
    safe = "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in raw
    )
    return safe[:128]


def _validate_runtime(
    evidence: ContainerEvidence,
    contract: ContainerContract,
    policy: EffectiveContainerPolicy,
    image_digest: str,
) -> None:
    tmpfs = _tmpfs_options(evidence.tmpfs)
    output_size = max(policy.stdout_bytes, policy.stderr_bytes) * 4
    checks = {
        "network mode": evidence.network_mode == "none",
        "read-only rootfs": evidence.readonly_rootfs,
        "runtime user": evidence.user == contract.user,
        "capability drop": "ALL" in evidence.cap_drop,
        "no-new-privileges": "no-new-privileges" in evidence.security_options,
        "memory limit": evidence.memory_bytes == policy.memory_mb * 1024 * 1024,
        "swap limit": evidence.memory_swap_bytes
        == policy.memory_mb * 1024 * 1024,
        "CPU limit": evidence.nano_cpus == int(policy.cpus * 1_000_000_000),
        "PID limit": evidence.pids_limit == policy.pids,
        "image digest": evidence.image_digest == image_digest,
        "log driver": evidence.log_driver == "none",
        "nofile ulimit": "nofile:256:256" in evidence.ulimits,
        "stop timeout": evidence.stop_timeout == 1,
        "output tmpfs": {
            "rw",
            "nosuid",
            "nodev",
            "noexec",
            "uid=10001",
            "gid=10001",
            "mode=0770",
            f"size={output_size}",
        }.issubset(tmpfs.get("/output", set())),
        "temporary tmpfs": {
            "rw",
            "nosuid",
            "nodev",
            "noexec",
            "size=64m",
        }.issubset(tmpfs.get("/tmp", set())),
        "bind mount allowlist": {
            mount.destination
            for mount in evidence.mounts
            if mount.mount_type == "bind"
        }
        == {contract.workdir, "/runner", "/run/iab"},
        "read-only bind mounts": all(
            not mount.writable
            for mount in evidence.mounts
            if mount.mount_type == "bind"
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ContainerInfrastructureError(
            "container runtime policy mismatch: " + ", ".join(failed)
        )


def _tmpfs_options(values: tuple[str, ...]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for value in values:
        destination, separator, options = value.partition(":")
        if not separator:
            continue
        result[destination] = set(options.split(","))
    return result


_LIVE_COLLECTOR = r"""
import base64, hashlib, json, os, stat, sys
limit = int(sys.argv[1])
if set(os.listdir("/output")) != {"result.json", "return.json"}:
    raise SystemExit("unexpected output files")
items = {}
for name in ("result.json", "return.json"):
    fd = os.open("/output/" + name, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise SystemExit("invalid artifact")
        payload = os.read(fd, limit + 1)
        if len(payload) > limit or os.read(fd, 1):
            raise SystemExit("oversized artifact")
    finally:
        os.close(fd)
    items[name] = {
        "payload": base64.b64encode(payload).decode("ascii"),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
    }
print(json.dumps(items, separators=(",", ":"), sort_keys=True))
"""


def _collect_live_artifacts(
    client: DockerClient, container_id: str, limit: int
) -> tuple[dict[str, Any], ArtifactEvidence]:
    completed = client.run(
        [
            "exec",
            "--user=10001:10001",
            container_id,
            "python",
            "-I",
            "-c",
            _LIVE_COLLECTOR,
            str(limit),
        ]
    )
    try:
        envelope = json.loads(completed.stdout)
        public = envelope["result.json"]
        private = envelope["return.json"]
        public_payload = base64.b64decode(public["payload"], validate=True)
        private_payload = base64.b64decode(private["payload"], validate=True)
        result = json.loads(public_payload)
        returned = json.loads(private_payload)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OutputCollectionError(f"invalid live artifact envelope: {exc}") from exc
    if not isinstance(result, dict) or result != returned:
        raise OutputCollectionError("result and return artifacts must match objects")
    if public["uid"] != 10001 or public["mode"] not in {0o600, 0o644}:
        raise OutputCollectionError("result artifact owner or mode is invalid")
    if hashlib.sha256(public_payload).hexdigest() != public["sha256"]:
        raise OutputCollectionError("result artifact hash mismatch")
    return result, ArtifactEvidence(
        filename="result.json",
        size_bytes=public["size"],
        sha256=public["sha256"],
        uid=public["uid"],
        gid=public["gid"],
        mode=public["mode"],
    )
