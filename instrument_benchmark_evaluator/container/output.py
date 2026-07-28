from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any


class OutputCollectionError(ValueError):
    """Candidate output cannot be accepted as a trusted artifact."""


@dataclass(frozen=True)
class ArtifactEvidence:
    filename: str
    size_bytes: int
    sha256: str
    uid: int
    gid: int
    mode: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "uid": self.uid,
            "gid": self.gid,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class CollectedResult:
    result: dict[str, Any]
    artifact: ArtifactEvidence
    return_artifact: ArtifactEvidence | None


def collect_result(
    output_dir: Path,
    filename: str,
    max_bytes: int,
    *,
    return_filename: str | None = None,
    expected_uid: int | None = None,
    allowed_modes: tuple[int, ...] = (0o600, 0o644),
    strict_output: bool = True,
) -> CollectedResult:
    root = output_dir.resolve()
    _safe_filename(filename)
    if return_filename is not None:
        _safe_filename(return_filename)
        if return_filename == filename:
            raise OutputCollectionError("return filename must be distinct")
    allowed = {filename}
    if return_filename:
        allowed.add(return_filename)
    if strict_output:
        try:
            actual = {entry.name for entry in os.scandir(root)}
        except OSError as exc:
            raise OutputCollectionError(f"cannot scan output directory: {exc}") from exc
        unexpected = actual - allowed
        if unexpected:
            raise OutputCollectionError(
                f"unexpected output files: {sorted(unexpected)!r}"
            )
    result, artifact = _read_json_object(
        root / filename, max_bytes, expected_uid, allowed_modes
    )
    return_artifact = None
    if return_filename is not None:
        returned, return_artifact = _read_json_object(
            root / return_filename, max_bytes, expected_uid, allowed_modes
        )
        if returned != result:
            raise OutputCollectionError("return artifact mismatch")
    return CollectedResult(result, artifact, return_artifact)


def _read_json_object(
    path: Path,
    max_bytes: int,
    expected_uid: int | None,
    allowed_modes: tuple[int, ...],
) -> tuple[dict[str, Any], ArtifactEvidence]:
    if max_bytes <= 0:
        raise OutputCollectionError("max_bytes must be positive")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OutputCollectionError(f"cannot open result artifact: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OutputCollectionError("result artifact must be a regular file")
        if metadata.st_size > max_bytes:
            raise OutputCollectionError("result artifact exceeds size limit")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode not in allowed_modes:
            raise OutputCollectionError(f"result artifact mode {mode:o} is forbidden")
        if expected_uid is not None and metadata.st_uid != expected_uid:
            raise OutputCollectionError("result artifact owner is invalid")
        payload = os.read(descriptor, max_bytes + 1)
        if len(payload) > max_bytes:
            raise OutputCollectionError("result artifact exceeds size limit")
        if os.read(descriptor, 1):
            raise OutputCollectionError("result artifact changed during collection")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutputCollectionError(f"result artifact is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise OutputCollectionError("result artifact must contain an object")
    evidence = ArtifactEvidence(
        filename=path.name,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        mode=mode,
    )
    return value, evidence


def _safe_filename(filename: str) -> None:
    if (
        not isinstance(filename, str)
        or not filename
        or PurePath(filename).name != filename
    ):
        raise OutputCollectionError("artifact filename must be a basename")
