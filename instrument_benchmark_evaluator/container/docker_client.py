from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from .errors import ContainerCommandTimeout, ContainerInfrastructureError


@dataclass(frozen=True)
class DockerCommandResult:
    returncode: int
    stdout: str
    stderr: str


Executor = Callable[..., DockerCommandResult]


class DockerClient:
    def __init__(
        self,
        *,
        executable: str = "docker",
        executor: Executor | None = None,
        max_output_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.executable = executable
        self.executor = executor or _execute
        self.max_output_bytes = max_output_bytes

    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout: float | None = None,
        check: bool = True,
    ) -> DockerCommandResult:
        argv = [self.executable, *arguments]
        try:
            result = self.executor(
                argv,
                timeout=timeout,
                max_output_bytes=self.max_output_bytes,
            )
        except FileNotFoundError as exc:
            raise ContainerInfrastructureError(
                f"Docker executable not found: {self.executable}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ContainerCommandTimeout(
                f"Docker command timed out: {' '.join(argv)}"
            ) from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ContainerInfrastructureError(
                f"Docker command failed ({result.returncode}): {detail}"
            )
        return result

    def inspect(self, container_id: str) -> dict:
        result = self.run(["inspect", container_id])
        return _one_inspect(result.stdout, "container inspect")

    def image_inspect(self, image_ref: str) -> dict:
        result = self.run(["image", "inspect", image_ref])
        return _one_inspect(result.stdout, "image inspect")

    def remove(self, container_id: str) -> DockerCommandResult:
        return self.run(["rm", "--force", container_id])


def _execute(
    argv: Sequence[str],
    *,
    timeout: float | None,
    max_output_bytes: int,
) -> DockerCommandResult:
    completed = subprocess.run(
        list(argv),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        timeout=timeout,
        check=False,
    )
    stdout = _decode_bounded(completed.stdout, max_output_bytes, "stdout")
    stderr = _decode_bounded(completed.stderr, max_output_bytes, "stderr")
    return DockerCommandResult(completed.returncode, stdout, stderr)


def _decode_bounded(payload: bytes, limit: int, stream: str) -> str:
    if len(payload) > limit:
        raise ContainerInfrastructureError(
            f"Docker command {stream} exceeded {limit} bytes"
        )
    return payload.decode("utf-8", errors="replace")


def _one_inspect(payload: str, label: str) -> dict:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ContainerInfrastructureError(f"malformed {label} JSON") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ContainerInfrastructureError(
            f"{label} must contain exactly one object"
        )
    return value[0]
