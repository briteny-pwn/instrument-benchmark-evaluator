from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .errors import ContainerCommandTimeout, ContainerInfrastructureError


@dataclass(frozen=True)
class DockerCommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class AttachedContainerResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    output_limited: bool
    completed_signal: bool


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

    def start_attached(
        self,
        container_id: str,
        *,
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
        artifact_callback: Callable[[], None] | None = None,
    ) -> AttachedContainerResult:
        argv = [self.executable, "start", "--attach", container_id]
        try:
            process = subprocess.Popen(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ContainerInfrastructureError(
                f"Docker executable not found: {self.executable}"
            ) from exc
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, ("stdout", stdout_limit))
        selector.register(process.stderr, selectors.EVENT_READ, ("stderr", stderr_limit))
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + timeout
        timed_out = False
        output_limited = False
        killed = False
        completed_signal = False
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0 and not killed:
                timed_out = True
                _detach_and_kill(process, self.executable, container_id)
                killed = True
                remaining = 1.0
            elif remaining <= 0:
                remaining = 0.1
            for key, _ in selector.select(max(0.0, min(remaining, 0.1))):
                stream, limit = key.data
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = buffers[stream]
                buffer.extend(chunk[: max(0, limit + 1 - len(buffer))])
                if len(buffer) > limit and not killed:
                    output_limited = True
                    _detach_and_kill(process, self.executable, container_id)
                    killed = True
                if (
                    stream == "stdout"
                    and not killed
                    and b"\n__IAB_BOOTSTRAP_COMPLETE_V1__\n"
                    in b"\n" + bytes(buffer)
                ):
                    if artifact_callback is not None:
                        artifact_callback()
                    completed_signal = True
                    _detach_and_kill(process, self.executable, container_id)
                    killed = True
        returncode = process.wait(timeout=5)
        selector.close()
        process.stdout.close()
        process.stderr.close()
        return AttachedContainerResult(
            returncode=returncode,
            stdout=bytes(buffers["stdout"][:stdout_limit]).decode(
                "utf-8", errors="replace"
            ),
            stderr=bytes(buffers["stderr"][:stderr_limit]).decode(
                "utf-8", errors="replace"
            ),
            timed_out=timed_out,
            output_limited=output_limited,
            completed_signal=completed_signal,
        )


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


def _kill_container(executable: str, container_id: str) -> None:
    completed = subprocess.run(
        [executable, "kill", container_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0:
        raise ContainerInfrastructureError(
            "failed to kill bounded candidate container: "
            + completed.stderr.decode("utf-8", errors="replace").strip()
        )


def _detach_and_kill(
    process: subprocess.Popen[bytes],
    executable: str,
    container_id: str,
) -> None:
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    _kill_container(executable, container_id)
