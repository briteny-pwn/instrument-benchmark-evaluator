#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any, Mapping

import yaml


from instrument_benchmark.contracts import dump_json, load_run_config
from instrument_benchmark.environment import RepositoryPaths, load_repository_paths
from instrument_benchmark.orchestrator import run_benchmark


EXPECTED_WORLDS = (
    "nominal",
    "small",
    "large",
    "needle_offset",
    "target_pose",
    "seeded_01",
    "seeded_02",
    "seeded_03",
    "seeded_04",
    "seeded_05",
)
EXPECTED_STEPS = ("step_1", "step_2", "step_3", "step_4")
EXPECTED_COMPONENTS = (
    "components/deposition.stl",
    "components/needle.stl",
    "components/sample.stl",
    "components/source.stl",
    "components/target.stl",
)
EXPECTED_ARTIFACTS = {
    "scene.glb",
    "scene.stl",
    "sem.png",
    "fib.png",
    *EXPECTED_COMPONENTS,
}
OPENFIBSEM_COMMIT = "2ebccb8b9721234ca66bb94de36d0f7cfe047af9"


class ValidationError(RuntimeError):
    """The distributed FIBSEM run does not satisfy its acceptance contract."""


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _regular_payload(path: Path) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValidationError(f"cannot inspect artifact: {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0:
        raise ValidationError(f"artifact is not a non-empty regular file: {path}")
    return path.read_bytes()


def bundle_digest(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise ValidationError(f"checkpoint bundle is invalid: {root}")
    records: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        records[relative] = hashlib.sha256(_regular_payload(path)).hexdigest()
    if "checkpoint.json" not in records:
        raise ValidationError("checkpoint bundle has no checkpoint.json")
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _validate_glb(payload: bytes) -> None:
    if len(payload) < 20:
        raise ValidationError("GLB is truncated")
    magic, version, declared = struct.unpack_from("<4sII", payload)
    if magic != b"glTF" or version != 2 or declared != len(payload):
        raise ValidationError("GLB header is invalid")
    chunk_length, chunk_type = struct.unpack_from("<I4s", payload, 12)
    if chunk_type != b"JSON" or 20 + chunk_length > len(payload):
        raise ValidationError("GLB JSON chunk is invalid")
    try:
        document = json.loads(payload[20 : 20 + chunk_length].rstrip(b" \0"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("GLB JSON cannot be parsed") from exc
    if not isinstance(document, dict) or document.get("asset", {}).get("version") != "2.0":
        raise ValidationError("GLB asset metadata is invalid")


def _validate_stl(payload: bytes) -> None:
    if len(payload) < 84:
        raise ValidationError("STL is truncated")
    triangles = struct.unpack_from("<I", payload, 80)[0]
    if triangles < 1 or len(payload) != 84 + triangles * 50:
        raise ValidationError("binary STL length is invalid")


def _validate_png(payload: bytes) -> tuple[int, int]:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValidationError("PNG signature is invalid")
    offset = 8
    width = height = None
    saw_end = False
    while offset + 12 <= len(payload):
        length = struct.unpack_from(">I", payload, offset)[0]
        kind = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            raise ValidationError("PNG chunk is truncated")
        data = payload[offset + 8 : offset + 8 + length]
        checksum = struct.unpack_from(">I", payload, offset + 8 + length)[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != checksum:
            raise ValidationError("PNG chunk checksum is invalid")
        if kind == b"IHDR":
            if length != 13 or width is not None:
                raise ValidationError("PNG IHDR is invalid")
            width, height = struct.unpack_from(">II", data)
        if kind == b"IEND":
            saw_end = True
            if end != len(payload):
                raise ValidationError("PNG has trailing bytes")
        offset = end
    if not saw_end or width is None or height is None or width < 1 or height < 1:
        raise ValidationError("PNG dimensions or terminator are invalid")
    return width, height


def validate_checkpoint_bundle(
    root: Path,
    *,
    world_id: str,
    step_id: str,
    expected_digest: str,
) -> dict[str, str]:
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if not path.is_dir()
    }
    if actual_files != EXPECTED_ARTIFACTS | {"checkpoint.json"}:
        raise ValidationError(f"checkpoint file set is invalid: {world_id}/{step_id}")
    if bundle_digest(root) != expected_digest:
        raise ValidationError(f"checkpoint bundle hash mismatch: {world_id}/{step_id}")
    try:
        checkpoint = json.loads(_regular_payload(root / "checkpoint.json"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("checkpoint JSON is invalid") from exc
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("schema_version") != 1
        or checkpoint.get("world_id") != world_id
        or checkpoint.get("step_id") != step_id
        or not _digest(checkpoint.get("geometry_hash"))
    ):
        raise ValidationError(f"checkpoint identity is invalid: {world_id}/{step_id}")
    artifacts = checkpoint.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != EXPECTED_ARTIFACTS:
        raise ValidationError(f"checkpoint artifact index is invalid: {world_id}/{step_id}")
    payloads: dict[str, bytes] = {}
    for relative in sorted(EXPECTED_ARTIFACTS):
        payload = _regular_payload(root / relative)
        record = artifacts.get(relative)
        if (
            not isinstance(record, dict)
            or set(record) != {"bytes", "sha256"}
            or record.get("bytes") != len(payload)
            or record.get("sha256") != hashlib.sha256(payload).hexdigest()
        ):
            raise ValidationError(f"artifact hash mismatch: {world_id}/{step_id}/{relative}")
        payloads[relative] = payload
    _validate_glb(payloads["scene.glb"])
    _validate_stl(payloads["scene.stl"])
    for relative in EXPECTED_COMPONENTS:
        _validate_stl(payloads[relative])
    sem_size = _validate_png(payloads["sem.png"])
    fib_size = _validate_png(payloads["fib.png"])
    if sem_size != fib_size:
        raise ValidationError(f"SEM/FIB dimensions differ: {world_id}/{step_id}")
    return {
        "geometry": checkpoint["geometry_hash"],
        "sem": artifacts["sem.png"]["sha256"],
        "fib": artifacts["fib.png"]["sha256"],
    }


def _container_policy(
    evidence: object,
    *,
    user: str,
    image_digest: str,
    image_field: str = "image_digest",
    label: str,
) -> None:
    if not isinstance(evidence, Mapping):
        raise ValidationError(f"{label} container evidence is missing")
    if (
        evidence.get("user") != user
        or evidence.get(image_field) != image_digest
        or evidence.get("network_mode") != "none"
        or evidence.get("readonly_rootfs") is not True
        or "ALL" not in evidence.get("cap_drop", [])
        or "no-new-privileges" not in evidence.get("security_options", [])
        or evidence.get("cleanup_succeeded") is not True
    ):
        raise ValidationError(f"{label} container isolation is invalid")


def validate_distributed_report(
    report: Mapping[str, Any],
    *,
    report_path: Path,
) -> dict[str, object]:
    if (
        report.get("schema_version") != 5
        or report.get("source_id") != "openfibsem"
        or report.get("evaluator_id") != "fibsem_liftout_v1"
        or report.get("openfibsem_commit") != OPENFIBSEM_COMMIT
        or report.get("score") != 100.0
        or report.get("strict_pass") is not True
        or report.get("retry_eligible") is not False
        or report.get("evidence_confidence") != 1.0
    ):
        raise ValidationError("suite report is not a strict score-100 FIBSEM result")
    provenance = report.get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "instrument", "instance", "evaluator", "openfibsem"
    }:
        raise ValidationError("three-repository plus OpenFIBSEM provenance is incomplete")
    if provenance["openfibsem"].get("commit") != OPENFIBSEM_COMMIT:
        raise ValidationError("OpenFIBSEM provenance commit is invalid")
    orchestration = report.get("orchestration")
    if not isinstance(orchestration, Mapping):
        raise ValidationError("orchestration evidence is missing")
    evaluator_image = orchestration.get("evaluator_image")
    container_provenance = orchestration.get("container_provenance")
    if not isinstance(evaluator_image, Mapping) or not isinstance(container_provenance, Mapping):
        raise ValidationError("image provenance is incomplete")
    evaluator_image_id = evaluator_image.get("image_id")
    candidate_image_id = container_provenance.get("image_digest")
    if not _digest(str(evaluator_image_id).removeprefix("sha256:")) or not _digest(
        str(candidate_image_id).removeprefix("sha256:")
    ):
        raise ValidationError("container image identities are invalid")
    if (
        evaluator_image.get("openfibsem_commit") != OPENFIBSEM_COMMIT
        or not _digest(evaluator_image.get("openfibsem_source_sha256"))
    ):
        raise ValidationError("evaluator image OpenFIBSEM provenance is invalid")
    outer = orchestration.get("evaluator_container")
    _container_policy(
        outer,
        user="11001:11001",
        image_digest=str(evaluator_image_id),
        image_field="image_id",
        label="outer evaluator",
    )
    worlds = report.get("worlds")
    if not isinstance(worlds, list) or tuple(
        world.get("world_id") if isinstance(world, Mapping) else None for world in worlds
    ) != EXPECTED_WORLDS:
        raise ValidationError("world suite order is invalid")
    artifact_index = report.get("artifacts")
    artifact_root = report_path.with_suffix(".artifacts")
    if (
        not isinstance(artifact_index, Mapping)
        or artifact_index.get("root") != artifact_root.name
        or not isinstance(artifact_index.get("worlds"), Mapping)
        or set(artifact_index["worlds"]) != set(EXPECTED_WORLDS)
        or artifact_root.is_symlink()
        or not artifact_root.is_dir()
    ):
        raise ValidationError("published artifact index is invalid")
    projection: dict[str, object] = {"worlds": {}}
    checkpoint_count = 0
    for world in worlds:
        world_id = world["world_id"]
        if (
            world.get("strict_pass") is not True
            or world.get("score") != 100.0
            or world.get("retry_eligible") is not False
            or world.get("terminal", {}).get("safe") is not True
            or world.get("runtime", {}).get("isolation_verified") is not True
            or tuple(world.get("checkpoints", {})) != EXPECTED_STEPS
            or not all(world.get("strict_gates", {}).values())
        ):
            raise ValidationError(f"world is not a strict pass: {world_id}")
        _container_policy(
            world.get("candidate_container_evidence"),
            user="10001:10001",
            image_digest=str(candidate_image_id),
            label=f"candidate {world_id}",
        )
        _container_policy(
            world.get("sim_container_evidence"),
            user="11001:11001",
            image_digest=str(evaluator_image_id),
            label=f"simulator {world_id}",
        )
        trusted = world.get("trusted_evidence")
        if not isinstance(trusted, Mapping) or not _digest(trusted.get("scenario_digest")):
            raise ValidationError(f"trusted scenario evidence is invalid: {world_id}")
        step_projection: dict[str, object] = {}
        step_index = artifact_index["worlds"][world_id]
        if not isinstance(step_index, Mapping) or tuple(step_index) != EXPECTED_STEPS:
            raise ValidationError(f"published checkpoint order is invalid: {world_id}")
        for step_id in EXPECTED_STEPS:
            checkpoint = world["checkpoints"][step_id]
            record = step_index[step_id]
            expected_path = f"{world_id}/{step_id}"
            if (
                not isinstance(record, Mapping)
                or record.get("path") != expected_path
                or record.get("sha256") != checkpoint.get("artifact_digest")
            ):
                raise ValidationError(f"artifact/report binding is invalid: {expected_path}")
            parsed = validate_checkpoint_bundle(
                artifact_root / expected_path,
                world_id=world_id,
                step_id=step_id,
                expected_digest=record["sha256"],
            )
            geometry = checkpoint.get("geometry", {}).get("canonical_geometry_hash")
            if parsed["geometry"] != geometry:
                raise ValidationError(f"geometry/report binding is invalid: {expected_path}")
            step_projection[step_id] = parsed
            checkpoint_count += 1
        projection["worlds"][world_id] = {
            "score": world["score"],
            "strict_gates": world["strict_gates"],
            "partial_order": world["partial_order"],
            "scenario_digest": trusted["scenario_digest"],
            "steps": step_projection,
        }
    if checkpoint_count != 40:
        raise ValidationError("the suite does not contain forty checkpoints")
    projection.update(
        {
            "score": report["score"],
            "strict_pass": report["strict_pass"],
            "strict_gates": report["strict_gates"],
            "dimension_scores": report["dimension_scores"],
        }
    )
    return projection


def _repeat_config(
    source_path: Path,
    destination: Path,
    report_path: Path,
    repository_paths: RepositoryPaths,
) -> None:
    config = load_run_config(source_path, repository_paths)
    value = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    value.update(
        {
            "run_id": f"{config.run_id}-repeat",
            "candidate_path": str(config.candidate_path),
            "report_path": str(report_path),
            "openfibsem_checkout": str(config.openfibsem_checkout),
        }
    )
    destination.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def managed_containers() -> tuple[str, ...]:
    completed = subprocess.run(
        [
            "docker", "ps", "-a", "--filter", "label=iab.managed=true",
            "--format", "{{.ID}}",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ValidationError(completed.stderr.strip() or "cannot inspect Docker resources")
    return tuple(line for line in completed.stdout.splitlines() if line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="validate-fibsem-benchmark")
    parser.add_argument("--instrument-root", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
    )
    parser.add_argument("--allow-dirty", action="store_true")
    arguments = parser.parse_args(argv)
    instrument_root = arguments.instrument_root.resolve()
    config_path = arguments.config.resolve()
    if not (instrument_root / "src/instrument_benchmark").is_dir():
        raise ValidationError("instrument root is invalid")
    if not config_path.is_relative_to(instrument_root):
        raise ValidationError("FIBSEM config must belong to the instrument repository")
    repository_paths = load_repository_paths(instrument_root)
    config = load_run_config(config_path, repository_paths)
    if (config.source_id, config.evaluator_id) != (
        "openfibsem",
        "fibsem_liftout_v1",
    ):
        raise ValidationError(
            "validator requires the (openfibsem, fibsem_liftout_v1) "
            "source/evaluator identity"
        )
    if config.report_path.exists() or config.report_path.with_suffix(".artifacts").exists():
        raise ValidationError("configured FIBSEM report or artifact destination already exists")
    first = run_benchmark(
        config_path,
        instrument_checkout=instrument_root,
        repository_paths=repository_paths,
        allow_dirty=arguments.allow_dirty,
    )
    first_projection = validate_distributed_report(first, report_path=config.report_path)
    with tempfile.TemporaryDirectory(prefix="iab-fibsem-repeat-") as directory:
        temporary = Path(directory)
        repeat_config = temporary / "config.yaml"
        repeat_report = temporary / "report.json"
        _repeat_config(
            config_path,
            repeat_config,
            repeat_report,
            repository_paths,
        )
        second = run_benchmark(
            repeat_config,
            instrument_checkout=instrument_root,
            repository_paths=repository_paths,
            allow_dirty=arguments.allow_dirty,
        )
        second_projection = validate_distributed_report(
            second, report_path=repeat_report
        )
    reproducible = first_projection == second_projection
    stale = managed_containers()
    passed = reproducible and not stale
    first["validation"] = {
        "passed": passed,
        "semantic_reproducibility": reproducible,
        "world_count": len(EXPECTED_WORLDS),
        "checkpoint_count": len(EXPECTED_WORLDS) * len(EXPECTED_STEPS),
        "managed_containers_after_run": list(stale),
        "openfibsem_commit": OPENFIBSEM_COMMIT,
        "limitations": [
            "This acceptance result applies to simulation, not physical FIB-SEM safety.",
        ],
    }
    dump_json(config.report_path, first)
    print(
        json.dumps(
            {
                "passed": passed,
                "strict_pass": first["strict_pass"],
                "score": first["score"],
                "world_count": len(EXPECTED_WORLDS),
                "checkpoint_count": len(EXPECTED_WORLDS) * len(EXPECTED_STEPS),
                "semantic_reproducibility": reproducible,
                "report": str(config.report_path),
            },
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
