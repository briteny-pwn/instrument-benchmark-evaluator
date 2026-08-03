from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .contracts import ContainerContract, RUNTIME_USER
from .errors import ImagePolicyError


_DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_REMOTE = re.compile(r"^(?:https?|git|ssh)://", re.IGNORECASE)
_FORBIDDEN_BUILD_INPUTS = {
    "solution.py",
    "candidate",
    "evaluator",
    "instrument_service",
    "oracle",
    "pyvisa_sim",
    "service",
    "worlds",
    "simulator",
    ".git",
}
_WILDCARD_CHARACTERS = frozenset("*?[")


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
            sources = _build_sources(arguments, instruction, line_number)
            if any(_REMOTE.match(item) for item in sources):
                raise ImagePolicyError("remote ADD is forbidden")
            _validate_build_inputs(sources, contract, line_number)
        elif instruction == "COPY":
            sources = _build_sources(arguments, instruction, line_number)
            _validate_build_inputs(sources, contract, line_number)
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


def _build_sources(
    arguments: list[str], instruction: str, line_number: int
) -> list[str]:
    values = list(arguments)
    while values and values[0].startswith("--"):
        option = values.pop(0)
        if option == "--from" or option.startswith("--from="):
            raise ImagePolicyError(
                f"COPY from build stages is forbidden at line {line_number}"
            )
        if not (
            option.startswith("--chown=")
            or option.startswith("--chmod=")
        ):
            raise ImagePolicyError(
                f"unsupported {instruction} option at line {line_number}"
            )
    if len(values) < 2:
        raise ImagePolicyError(
            f"{instruction} requires a source and destination at line {line_number}"
        )
    if any(value.startswith("[") or value.endswith("]") for value in values):
        raise ImagePolicyError(
            f"JSON-form {instruction} is unsupported at line {line_number}"
        )
    return values[:-1]


def _validate_build_inputs(
    sources: list[str], contract: ContainerContract, line_number: int
) -> None:
    _reject_forbidden_inputs(sources, line_number)
    declared = tuple(PurePosixPath(item) for item in contract.context_files)
    for source in sources:
        if any(character in source for character in _WILDCARD_CHARACTERS):
            raise ImagePolicyError(
                f"COPY/ADD wildcards are forbidden at line {line_number}"
            )
        if "\\" in source:
            raise ImagePolicyError(
                f"invalid build-context path at line {line_number}"
            )
        relative = PurePosixPath(source)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or str(relative) in {"", "."}
        ):
            raise ImagePolicyError(
                f"invalid build-context path at line {line_number}"
            )
        if not any(
            candidate == relative or relative in candidate.parents
            for candidate in declared
        ):
            raise ImagePolicyError(
                f"source is outside declared context at line {line_number}"
            )
