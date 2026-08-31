#!/usr/bin/env python3
"""Vendor and verify the pinned OpenFIBSEM CPython 3.11 Linux wheel set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Iterable


OPENFIBSEM_COMMIT = "2ebccb8b9721234ca66bb94de36d0f7cfe047af9"
DEFAULT_PLATFORM = "manylinux_2_28_x86_64"
DEFAULT_PYTHON = "311"
LOCK_NAME = "openfibsem-requirements.lock"
MAX_GIT_FILE_BYTES = 90_000_000
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\\\s]+) \\$")
_HASH_LINE = re.compile(r"^\s+--hash=sha256:([0-9a-f]{64})$")


class VendorError(RuntimeError):
    """The source lock or vendored wheel bundle is invalid."""


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise VendorError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def _verify_source(source: Path) -> tuple[str, tuple[str, ...]]:
    source = source.resolve()
    if _git(source, "rev-parse", "--show-toplevel") != str(source):
        raise VendorError("OpenFIBSEM source must be a repository root")
    commit = _git(source, "rev-parse", "HEAD")
    if commit != OPENFIBSEM_COMMIT:
        raise VendorError("OpenFIBSEM source commit is not the pinned commit")
    for arguments in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
        completed = subprocess.run(
            ["git", *arguments],
            cwd=source,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode == 1:
            raise VendorError("OpenFIBSEM source has tracked modifications")
        if completed.returncode != 0:
            raise VendorError(
                completed.stderr.decode("utf-8", errors="replace").strip()
                or "cannot verify OpenFIBSEM source"
            )
    try:
        configuration = tomllib.loads((source / "pyproject.toml").read_text())
        runtime = configuration["project"]["dependencies"]
        build = configuration["build-system"]["requires"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise VendorError(f"cannot read OpenFIBSEM dependency lock: {exc}") from exc
    if (
        not isinstance(runtime, list)
        or not isinstance(build, list)
        or not all(isinstance(item, str) and item.strip() for item in runtime + build)
    ):
        raise VendorError("OpenFIBSEM dependencies are invalid")
    requirements = tuple(sorted(set(runtime + build), key=str.lower))
    return commit, requirements


def _wheel_metadata(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_paths = [
                name
                for name in archive.namelist()
                if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_paths) != 1:
                raise VendorError(f"wheel metadata is ambiguous: {path.name}")
            metadata = archive.read(metadata_paths[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise VendorError(f"cannot read wheel metadata: {path.name}: {exc}") from exc
    name = version = None
    for line in metadata.splitlines():
        if line.startswith("Name: "):
            name = line.removeprefix("Name: ").strip()
        elif line.startswith("Version: "):
            version = line.removeprefix("Version: ").strip()
        if name and version:
            break
    if not name or not version or any(character.isspace() for character in version):
        raise VendorError(f"wheel identity is invalid: {path.name}")
    return _normalize_name(name), version


def _wheel_platform(path: Path) -> str:
    try:
        _prefix, python_tag, abi_tag, platform_tag = path.stem.rsplit("-", 3)
    except ValueError as exc:
        raise VendorError(f"wheel filename is invalid: {path.name}") from exc
    python_tags = set(python_tag.split("."))
    abi_tags = set(abi_tag.split("."))
    if not (
        python_tags & {"cp311", "py3", "py311", "py2", "py2.py3"}
        or any(
            tag.startswith("cp3")
            and tag[3:].isdigit()
            and int(tag[3:]) <= 11
            and "abi3" in abi_tags
            for tag in python_tags
        )
    ):
        raise VendorError(f"wheel is not CPython 3.11 compatible: {path.name}")
    platforms = platform_tag.split(".")
    if platforms == ["any"]:
        return "any"
    for tag in platforms:
        if tag == "manylinux2014_x86_64":
            continue
        match = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", tag)
        if match and (int(match.group(1)), int(match.group(2))) <= (2, 28):
            continue
        raise VendorError(f"wheel platform exceeds manylinux_2_28_x86_64: {path.name}")
    return "manylinux_x86_64"


def _read_lock(path: Path) -> dict[str, tuple[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise VendorError(f"cannot read OpenFIBSEM requirement lock: {exc}") from exc
    records: dict[str, tuple[str, str]] = {}
    pending: tuple[str, str] | None = None
    for line in lines:
        if not line or line.startswith("#"):
            continue
        requirement = _REQUIREMENT.fullmatch(line)
        if requirement and pending is None:
            pending = (_normalize_name(requirement.group(1)), requirement.group(2))
            continue
        digest = _HASH_LINE.fullmatch(line)
        if digest and pending is not None:
            name, version = pending
            if name in records:
                raise VendorError(f"duplicate OpenFIBSEM requirement: {name}")
            records[name] = (version, digest.group(1))
            pending = None
            continue
        raise VendorError(f"invalid OpenFIBSEM lock line: {line!r}")
    if pending is not None or not records:
        raise VendorError("OpenFIBSEM requirement lock is incomplete")
    return records


def _manifest_records(wheelhouse: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    names: set[str] = set()
    for path in sorted(wheelhouse.glob("*.whl")):
        name, version = _wheel_metadata(path)
        if name in names:
            raise VendorError(f"multiple wheels resolve the same package: {name}")
        names.add(name)
        payload = path.read_bytes()
        records[path.name] = {
            "normalized_name": name,
            "version": version,
            "sha256": _sha256(payload),
            "bytes": len(payload),
            "platform": _wheel_platform(path),
        }
    if not records:
        raise VendorError("OpenFIBSEM wheelhouse is empty")
    return records


def _split_large_wheels(
    wheelhouse: Path,
    records: dict[str, dict[str, object]],
    *,
    max_file_bytes: int = MAX_GIT_FILE_BYTES,
) -> None:
    if max_file_bytes <= 0:
        raise VendorError("wheel part size must be positive")
    for filename, record in records.items():
        if int(record["bytes"]) <= max_file_bytes:
            continue
        wheel = wheelhouse / filename
        parts: list[dict[str, object]] = []
        with wheel.open("rb") as source:
            index = 0
            while payload := source.read(max_file_bytes):
                part = wheelhouse / f"{filename}.part{index:03d}"
                part.write_bytes(payload)
                parts.append(
                    {
                        "filename": part.name,
                        "sha256": _sha256(payload),
                        "bytes": len(payload),
                    }
                )
                index += 1
        if len(parts) < 2:
            raise VendorError(f"large wheel was not split: {filename}")
        record["parts"] = parts
        wheel.unlink()


def _verified_manifest_records(
    wheelhouse: Path,
    expected: dict[str, object],
) -> dict[str, dict[str, object]]:
    actual: dict[str, dict[str, object]] = {}
    expected_storage = {"manifest.json"}
    with tempfile.TemporaryDirectory(prefix=".verify-wheels.") as directory:
        reconstructed_root = Path(directory)
        for filename, value in expected.items():
            if not isinstance(filename, str) or not filename.endswith(".whl"):
                raise VendorError("OpenFIBSEM wheel manifest filename is invalid")
            if not isinstance(value, dict):
                raise VendorError("OpenFIBSEM wheel manifest record is invalid")
            parts = value.get("parts")
            if parts is None:
                if set(value) != {
                    "normalized_name",
                    "version",
                    "sha256",
                    "bytes",
                    "platform",
                }:
                    raise VendorError("OpenFIBSEM wheel manifest record is invalid")
                path = wheelhouse / filename
                expected_storage.add(filename)
            else:
                if (
                    set(value)
                    != {
                        "normalized_name",
                        "version",
                        "sha256",
                        "bytes",
                        "platform",
                        "parts",
                    }
                    or not isinstance(parts, list)
                    or len(parts) < 2
                ):
                    raise VendorError("OpenFIBSEM wheel part manifest is invalid")
                path = reconstructed_root / filename
                full_digest = hashlib.sha256()
                full_size = 0
                with path.open("wb") as destination:
                    for index, part_record in enumerate(parts):
                        if (
                            not isinstance(part_record, dict)
                            or set(part_record) != {"filename", "sha256", "bytes"}
                            or part_record.get("filename")
                            != f"{filename}.part{index:03d}"
                            or not _DIGEST.fullmatch(str(part_record.get("sha256", "")))
                            or isinstance(part_record.get("bytes"), bool)
                            or not isinstance(part_record.get("bytes"), int)
                            or part_record["bytes"] <= 0
                        ):
                            raise VendorError("OpenFIBSEM wheel part record is invalid")
                        part = wheelhouse / part_record["filename"]
                        if part.is_symlink() or not part.is_file():
                            raise VendorError("OpenFIBSEM wheel part is not a regular file")
                        payload = part.read_bytes()
                        if (
                            len(payload) != part_record["bytes"]
                            or _sha256(payload) != part_record["sha256"]
                        ):
                            raise VendorError("OpenFIBSEM wheel part hash mismatch")
                        destination.write(payload)
                        full_digest.update(payload)
                        full_size += len(payload)
                        expected_storage.add(part.name)
                if (
                    full_size != value.get("bytes")
                    or full_digest.hexdigest() != value.get("sha256")
                ):
                    raise VendorError("OpenFIBSEM reconstructed wheel hash mismatch")
            if path.is_symlink() or not path.is_file():
                raise VendorError("OpenFIBSEM wheel is not a regular file")
            record = _manifest_records(path.parent).get(filename)
            if record is None:
                raise VendorError("OpenFIBSEM wheel metadata is missing")
            if parts is not None:
                record["parts"] = parts
            actual[filename] = record
    actual_storage = {path.name for path in wheelhouse.iterdir()}
    if actual_storage != expected_storage:
        raise VendorError("OpenFIBSEM wheelhouse contains unexpected files")
    return actual


def _lock_payload(records: dict[str, dict[str, object]]) -> bytes:
    by_name = sorted(
        records.values(), key=lambda record: str(record["normalized_name"])
    )
    lines = [
        "# Generated by scripts/vendor_openfibsem_wheels.py; do not edit.",
        "# CPython 3.11 / manylinux_2_28_x86_64-compatible wheels only.",
    ]
    for record in by_name:
        lines.extend(
            [
                f"{record['normalized_name']}=={record['version']} \\",
                f"    --hash=sha256:{record['sha256']}",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def verify_vendor_bundle(
    wheelhouse: Path,
    lock_path: Path,
    *,
    source_commit: str,
    platform: str,
    python_version: str,
) -> dict[str, object]:
    manifest_path = wheelhouse / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VendorError(f"cannot read OpenFIBSEM wheel manifest: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("source_commit") != source_commit
        or manifest.get("platform") != platform
        or manifest.get("python_version") != python_version
        or not isinstance(manifest.get("source_requirements_sha256"), str)
        or not _DIGEST.fullmatch(manifest["source_requirements_sha256"])
        or not isinstance(manifest.get("files"), dict)
    ):
        raise VendorError("OpenFIBSEM wheel manifest identity is invalid")
    actual = _verified_manifest_records(wheelhouse, manifest["files"])
    if manifest["files"] != actual:
        raise VendorError("OpenFIBSEM wheel manifest hash or metadata mismatch")
    lock = _read_lock(lock_path)
    expected_lock = {
        str(record["normalized_name"]): (
            str(record["version"]),
            str(record["sha256"]),
        )
        for record in actual.values()
    }
    if lock != expected_lock:
        raise VendorError("OpenFIBSEM requirement lock does not match wheels")
    return manifest


def vendor(
    source: Path,
    destination: Path,
    lock_path: Path,
    *,
    platform: str,
    python_version: str,
) -> None:
    commit, requirements = _verify_source(source)
    if destination.exists() or lock_path.exists():
        raise VendorError("destination and requirement lock must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    requirements_digest = _sha256(_canonical_json(requirements))
    with tempfile.TemporaryDirectory(
        prefix=".openfibsem-wheelhouse.", dir=destination.parent
    ) as temporary_directory:
        temporary = Path(temporary_directory)
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--dest",
            str(temporary),
            "--platform",
            platform,
            "--platform",
            "manylinux_2_17_x86_64",
            "--platform",
            "manylinux2014_x86_64",
            "--python-version",
            python_version,
            "--implementation",
            "cp",
            "--abi",
            f"cp{python_version}",
            "--abi",
            "abi3",
            "--abi",
            "none",
            *requirements,
        ]
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-30:])
            raise VendorError(f"pip could not resolve binary runtime wheels:\n{tail}")
        records = _manifest_records(temporary)
        _split_large_wheels(temporary, records)
        manifest = {
            "schema_version": 1,
            "source_commit": commit,
            "source_requirements_sha256": requirements_digest,
            "platform": platform,
            "python_version": python_version,
            "files": records,
        }
        (temporary / "manifest.json").write_bytes(_canonical_json(manifest))
        temporary_lock = temporary.parent / f".{lock_path.name}.{os.getpid()}.tmp"
        try:
            temporary_lock.write_bytes(_lock_payload(records))
            verify_vendor_bundle(
                temporary,
                temporary_lock,
                source_commit=commit,
                platform=platform,
                python_version=python_version,
            )
            os.replace(temporary, destination)
            os.replace(temporary_lock, lock_path)
        finally:
            temporary_lock.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--platform", default=DEFAULT_PLATFORM)
    parser.add_argument("--python-version", default=DEFAULT_PYTHON)
    parser.add_argument("--verify", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.platform != DEFAULT_PLATFORM or arguments.python_version != DEFAULT_PYTHON:
        raise VendorError("only CPython 3.11 manylinux_2_28_x86_64 is supported")
    source, requirements = _verify_source(arguments.source)
    lock_path = arguments.lock or arguments.destination.parent / LOCK_NAME
    if arguments.verify:
        manifest = verify_vendor_bundle(
            arguments.destination,
            lock_path,
            source_commit=source,
            platform=arguments.platform,
            python_version=arguments.python_version,
        )
        expected = _sha256(_canonical_json(requirements))
        if manifest.get("source_requirements_sha256") != expected:
            raise VendorError("OpenFIBSEM source dependencies changed")
    else:
        vendor(
            arguments.source,
            arguments.destination,
            lock_path,
            platform=arguments.platform,
            python_version=arguments.python_version,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VendorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
