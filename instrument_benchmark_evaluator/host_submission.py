from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import InstanceSettings


BOOTSTRAP = Path(__file__).with_name("bootstrap.py")


@dataclass(frozen=True)
class ProcessResult:
    status: str
    returncode: int | None
    stdout: str
    stderr: str
    result: dict[str, Any] | None


def invoke_candidate(
    workspace: Path,
    endpoint: Path,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
    python_executable: str = sys.executable,
    solution_filename: str = "solution.py",
    result_filename: str = "result.json",
) -> ProcessResult:
    workspace = workspace.resolve()
    output_path = workspace / result_filename
    return_path = workspace / ".candidate_return.json"
    command = [
        python_executable,
        "-I",
        str(BOOTSTRAP),
        str((workspace / solution_filename).resolve()),
        str(endpoint),
        str(output_path),
        str(return_path),
    ]
    process = subprocess.Popen(
        command,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "0"},
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        return ProcessResult(
            "candidate_timeout",
            None,
            _limit(stdout, max_output_bytes),
            _limit(stderr, max_output_bytes),
            None,
        )
    stdout = _limit(stdout, max_output_bytes)
    stderr = _limit(stderr, max_output_bytes)
    if len(stdout.encode()) >= max_output_bytes or len(stderr.encode()) >= max_output_bytes:
        return ProcessResult("output_limit", process.returncode, stdout, stderr, None)
    if process.returncode == 3:
        return ProcessResult("invalid_result", 3, stdout, stderr, None)
    if process.returncode != 0:
        return ProcessResult(
            "candidate_failure", process.returncode, stdout, stderr, None
        )
    try:
        result = json.loads(return_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ProcessResult(
            "invalid_result",
            process.returncode,
            stdout,
            f"{stderr}\ninvalid private return artifact: {exc}".strip(),
            None,
        )
    if not isinstance(result, dict):
        return ProcessResult(
            "invalid_result",
            process.returncode,
            stdout,
            f"{stderr}\nprivate return artifact is not an object".strip(),
            None,
        )
    return ProcessResult("completed", process.returncode, stdout, stderr, result)


class HostCandidateBackend:
    """Test-only backend retaining the legacy host subprocess execution."""

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
    ) -> ProcessResult:
        del candidate_path, run_id, world_id
        return invoke_candidate(
            workspace,
            endpoint,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            solution_filename=instance.submission_filename,
            result_filename=instance.result_filename,
        )


def _limit(value: str, max_bytes: int) -> str:
    payload = value.encode("utf-8", errors="replace")
    if len(payload) <= max_bytes:
        return value
    return payload[:max_bytes].decode("utf-8", errors="replace")
