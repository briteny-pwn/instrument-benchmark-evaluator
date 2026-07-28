from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from .container.contracts import EvaluatorMaxima, effective_policy
from .container.docker_client import DockerClient
from .container.errors import ContainerInfrastructureError
from .container.image import ImageEvidence, build_image, resolve_image
from .container.runner import ContainerProcessResult, run_container
from .contracts import InstanceSettings
from .host_submission import ProcessResult


CandidateProcessResult = ProcessResult | ContainerProcessResult


class CandidateBackend(Protocol):
    def invoke(
        self,
        *,
        workspace: Path,
        candidate_path: Path,
        endpoint: Path,
        instance: InstanceSettings,
        timeout_seconds: float,
        max_output_bytes: int,
        run_id: str,
        world_id: str,
    ) -> CandidateProcessResult: ...


class DockerCandidateBackend:
    def __init__(
        self,
        *,
        client: DockerClient,
        image: ImageEvidence,
        runner_dir: Path | None = None,
    ) -> None:
        self.client = client
        self.image = image
        self.runner_dir = (
            runner_dir or Path(__file__).resolve().parent
        ).resolve()

    @classmethod
    def from_instance(
        cls,
        instance: InstanceSettings,
        *,
        client: DockerClient | None = None,
    ) -> DockerCandidateBackend:
        docker = client or DockerClient()
        try:
            image = resolve_image(
                instance.container,
                docker,
                instance_id=instance.instance_id,
            )
        except ContainerInfrastructureError:
            image = build_image(
                instance.container,
                docker,
                instance_id=instance.instance_id,
            )
        return cls(client=docker, image=image)

    def invoke(
        self,
        *,
        workspace: Path,
        candidate_path: Path,
        endpoint: Path,
        instance: InstanceSettings,
        timeout_seconds: float,
        max_output_bytes: int,
        run_id: str,
        world_id: str,
    ) -> ContainerProcessResult:
        del candidate_path
        output_dir = workspace.parent / "output"
        output_dir.mkdir(mode=0o777)
        output_dir.chmod(0o777)
        staged_runner = _stage_runner(self.runner_dir, workspace.parent / "runner")
        return run_container(
            contract=instance.container,
            policy=effective_policy(
                instance.container,
                EvaluatorMaxima(
                    timeout_seconds=timeout_seconds,
                    stdout_bytes=max_output_bytes,
                    stderr_bytes=max_output_bytes,
                ),
            ),
            image_digest=self.image.image_id,
            workspace=workspace,
            output_dir=output_dir,
            gateway_socket=endpoint,
            runner_dir=staged_runner,
            client=self.client,
            run_id=run_id,
            world_id=world_id,
            expected_output_uid=10001,
        )


def _stage_runner(source: Path, destination: Path) -> Path:
    required = (
        Path("bootstrap.py"),
        Path("container") / "bootstrap_contract.py",
    )
    destination.mkdir(mode=0o755)
    for relative in required:
        source_file = source / relative
        if source_file.is_symlink() or not source_file.is_file():
            raise ContainerInfrastructureError(
                f"candidate bootstrap input is invalid: {relative}"
            )
        target = destination / relative
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
        target.chmod(0o644)
    return destination.resolve()
