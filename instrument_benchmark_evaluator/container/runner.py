from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .contracts import ContainerContract, EffectiveContainerPolicy
from .docker_client import DockerClient
from .errors import ContainerCommandTimeout, ContainerInfrastructureError
from .evidence import ContainerEvidence, normalize_inspect
from .output import ArtifactEvidence, OutputCollectionError, collect_result


@dataclass(frozen=True)
class ContainerProcessResult:
    status: str
    returncode: int | None
    stdout: str
    stderr: str
    result: dict[str, Any] | None
    container_evidence: ContainerEvidence
    artifact_evidence: ArtifactEvidence | None


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
            )
        )
        container_id = created.stdout.strip()
        if not container_id or "\n" in container_id:
            raise ContainerInfrastructureError(
                "docker create did not return one container ID"
            )
        client.run(["start", container_id])
        timed_out = False
        try:
            waited = client.run(
                ["wait", container_id], timeout=policy.timeout_seconds
            )
        except ContainerCommandTimeout:
            timed_out = True
            client.run(["kill", container_id], check=False)
            waited = client.run(["wait", container_id], check=False)
        returncode = _returncode(waited.stdout)
        logs = client.run(["logs", container_id], check=False)
        stdout = _bounded(logs.stdout, policy.stdout_bytes)
        stderr = _bounded(logs.stderr, policy.stderr_bytes)
        evidence = normalize_inspect(client.inspect(container_id))
        if timed_out:
            status = "candidate_timeout"
        elif evidence.oom_killed:
            status = "oom_killed"
        elif (
            len(logs.stdout.encode("utf-8", errors="replace"))
            > policy.stdout_bytes
            or len(logs.stderr.encode("utf-8", errors="replace"))
            > policy.stderr_bytes
        ):
            status = "output_limit"
        elif returncode == 0:
            try:
                collected = collect_result(
                    output_dir,
                    "result.json",
                    policy.stdout_bytes,
                    return_filename="return.json",
                    expected_uid=expected_output_uid,
                )
            except OutputCollectionError as exc:
                status = "invalid_result"
                stderr = f"{stderr}\ninvalid result: {exc}".strip()
            else:
                status = "completed"
                result = collected.result
                artifact = collected.artifact
        elif returncode in {2, 3}:
            status = "invalid_result"
        else:
            status = "candidate_failure"
    finally:
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
) -> list[str]:
    solution = f"{contract.workdir}/solution.py"
    returned = str(Path(contract.output_path).with_name("return.json"))
    return [
        "create",
        f"--name={name}",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--user={contract.user}",
        f"--workdir={contract.workdir}",
        f"--cpus={policy.cpus}",
        f"--memory={policy.memory_mb}m",
        f"--pids-limit={policy.pids}",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
        f"--mount=type=bind,src={workspace.resolve()},dst={contract.workdir},readonly",
        f"--mount=type=bind,src={runner_dir.resolve()},dst=/runner,readonly",
        (
            "--mount=type=bind,"
            f"src={gateway_socket.parent.resolve()},dst=/run/iab,readonly"
        ),
        f"--mount=type=bind,src={output_dir.resolve()},dst=/output",
        image_digest,
        solution,
        contract.gateway_path,
        contract.output_path,
        returned,
    ]


def _returncode(payload: str) -> int:
    try:
        return int(payload.strip())
    except ValueError as exc:
        raise ContainerInfrastructureError(
            f"docker wait returned invalid exit code: {payload!r}"
        ) from exc


def _bounded(value: str, maximum: int) -> str:
    payload = value.encode("utf-8", errors="replace")
    return payload[:maximum].decode("utf-8", errors="replace")


def _container_name(run_id: str, world_id: str) -> str:
    raw = f"iab-{run_id}-{world_id}"
    safe = "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in raw
    )
    return safe[:128]
