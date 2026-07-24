from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .contracts import ContainerContract, RUNTIME_USER
from .errors import ImagePolicyError


_DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_REMOTE = re.compile(r"^(?:https?|git|ssh)://", re.IGNORECASE)
_FORBIDDEN_BUILD_INPUTS = {
    "solution.py",
    "candidate",
    "evaluator",
    "oracle",
    "worlds",
    "simulator",
    ".git",
}


@dataclass(frozen=True)
class DockerfileEvidence:
    dockerfile_sha256: str
    base_images: tuple[str, ...]
    final_user: str


def validate_dockerfile(
    path: Path, contract: ContainerContract
) -> DockerfileEvidence:
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ImagePolicyError(f"cannot read Dockerfile: {exc}") from exc
    digest = hashlib.sha256(payload).hexdigest()
    if digest != contract.lock.dockerfile_sha256:
        raise ImagePolicyError("Dockerfile hash does not match image lock")
    logical = _logical_lines(text)
    base_images: list[str] = []
    final_user: str | None = None
    for line_number, line in logical:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            fields = shlex.split(stripped, comments=True, posix=True)
        except ValueError as exc:
            raise ImagePolicyError(
                f"cannot parse Dockerfile line {line_number}: {exc}"
            ) from exc
        if not fields:
            continue
        instruction = fields[0].upper()
        arguments = fields[1:]
        if instruction == "FROM":
            image = _from_image(arguments, line_number)
            if not _DIGEST_IMAGE.fullmatch(image):
                raise ImagePolicyError(
                    f"FROM line {line_number} must be digest-pinned"
                )
            base_images.append(image)
        elif instruction == "ADD":
            if any(_REMOTE.match(item) for item in arguments[:-1]):
                raise ImagePolicyError("remote ADD is forbidden")
            _reject_forbidden_inputs(arguments[:-1], line_number)
        elif instruction == "COPY":
            sources = [
                item
                for item in arguments[:-1]
                if not item.startswith("--")
            ]
            _reject_forbidden_inputs(sources, line_number)
        elif instruction == "USER":
            if len(arguments) != 1:
                raise ImagePolicyError(f"invalid USER at line {line_number}")
            final_user = arguments[0]
    if not base_images:
        raise ImagePolicyError("Dockerfile must contain a digest-pinned FROM")
    if final_user != RUNTIME_USER:
        raise ImagePolicyError(f"final USER {RUNTIME_USER} is required")
    return DockerfileEvidence(
        dockerfile_sha256=digest,
        base_images=tuple(base_images),
        final_user=final_user,
    )


def _logical_lines(text: str) -> tuple[tuple[int, str], ...]:
    result: list[tuple[int, str]] = []
    pending = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if not pending:
            start = number
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1] + " "
            continue
        result.append((start, pending + raw))
        pending = ""
    if pending:
        raise ImagePolicyError("unterminated Dockerfile continuation")
    return tuple(result)


def _from_image(arguments: list[str], line_number: int) -> str:
    filtered = list(arguments)
    while filtered and filtered[0].startswith("--"):
        filtered.pop(0)
    if not filtered:
        raise ImagePolicyError(f"missing FROM image at line {line_number}")
    return filtered[0]


def _reject_forbidden_inputs(sources: list[str], line_number: int) -> None:
    for source in sources:
        lowered_parts = {
            part.lower()
            for part in Path(source).parts
        }
        if lowered_parts & _FORBIDDEN_BUILD_INPUTS:
            raise ImagePolicyError(
                f"candidate source or hidden material at line {line_number}"
            )
