from __future__ import annotations

import argparse
import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from .geometry.roi import RoiSet, derive_roi_set
from .geometry.stl_mesh import CanonicalMesh, StlError, StlLimits, parse_stl
from .models import ScenarioSpec


REFERENCE_SCHEMA_VERSION = 1
MESH_PARSER_VERSION = "canonical-stl-v1"
SHAPE_ALGORITHM_VERSION = "stl-shape-v1"
STEP_IDS = ("step_1", "step_2", "step_3", "step_4")
REFERENCE_ARTIFACT_DIRECTORY = Path(__file__).with_name("reference_artifacts")
REFERENCE_FILES = ("baseline/sample.stl",) + tuple(
    f"{step}/{name}"
    for step in STEP_IDS
    for name in ("sample.stl", "deposition.stl")
)

__all__ = [
    "ReferenceBundle",
    "ReferenceBundleError",
    "ReferenceIdentity",
    "ReferenceStep",
    "RoiSet",
    "build_reference_bundle",
    "derive_roi_set",
    "load_reference_bundle",
    "load_packaged_reference_bundles",
    "main",
]


class ReferenceBundleError(RuntimeError):
    """A private reference bundle is missing, corrupt, or incompatible."""


@dataclass(frozen=True)
class ReferenceIdentity:
    schema_version: int
    source_id: str
    evaluator_id: str
    scenario_id: str
    scenario_sha256: str
    openfibsem_commit: str
    evaluator_commit: str
    generator_tree_sha256: str
    reference_solution_sha256: str
    mesh_parser_version: str
    algorithm_version: str
    parameter_sha256: str
    bundle_sha256: str
    file_sha256: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "evaluator_id": self.evaluator_id,
            "scenario_id": self.scenario_id,
            "scenario_sha256": self.scenario_sha256,
            "openfibsem_commit": self.openfibsem_commit,
            "evaluator_commit": self.evaluator_commit,
            "generator_tree_sha256": self.generator_tree_sha256,
            "reference_solution_sha256": self.reference_solution_sha256,
            "mesh_parser_version": self.mesh_parser_version,
            "algorithm_version": self.algorithm_version,
            "parameter_sha256": self.parameter_sha256,
            "bundle_sha256": self.bundle_sha256,
            "file_sha256": dict(sorted(self.file_sha256.items())),
        }


@dataclass(frozen=True)
class ReferenceStep:
    step_id: str
    root: Path
    sample: CanonicalMesh
    deposition: CanonicalMesh


@dataclass(frozen=True)
class ReferenceBundle:
    root: Path
    identity: ReferenceIdentity
    scenario_document: Mapping[str, object]
    baseline_sample: CanonicalMesh
    steps: Mapping[str, ReferenceStep]


def load_packaged_reference_bundles(
    specs: Sequence[ScenarioSpec],
) -> Mapping[str, ReferenceBundle]:
    bundles: dict[str, ReferenceBundle] = {}
    for spec in specs:
        if spec.scenario_id in bundles:
            raise ReferenceBundleError("duplicate packaged reference scenario")
        bundles[spec.scenario_id] = load_reference_bundle(
            REFERENCE_ARTIFACT_DIRECTORY / spec.scenario_id,
            spec,
        )
    return MappingProxyType(dict(sorted(bundles.items())))


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_sha256(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReferenceBundleError(f"reference {name} digest is invalid")
    return value


def _validate_commit(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReferenceBundleError(f"reference {name} commit is invalid")
    return value


def _parameters() -> dict[str, object]:
    return {
        "mesh_parser_version": MESH_PARSER_VERSION,
        "shape_algorithm_version": SHAPE_ALGORITHM_VERSION,
        "voxel": {
            "relative_edge": 0.02,
            "minimum_edge_um": 0.1,
            "maximum_edge_um": 0.5,
            "maximum_cells": 4_194_304,
        },
        "surface_sampling": {
            "minimum_samples": 2_048,
            "maximum_samples": 32_768,
        },
        "shape_weights": {
            "volume_similarity": 0.25,
            "voxel_iou": 0.35,
            "asd_score": 0.25,
            "hausdorff_score": 0.15,
        },
    }


def _regular_payload(path: Path, *, maximum_bytes: int = 64 * 1024 * 1024) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReferenceBundleError(f"reference file is missing: {path.name}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ReferenceBundleError("reference path must be a regular file")
    if metadata.st_size > maximum_bytes:
        raise ReferenceBundleError("reference file exceeds the size limit")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ReferenceBundleError(f"cannot read reference file: {path.name}") from exc


def _mesh_index(root: Path) -> tuple[dict[str, dict[str, object]], dict[str, CanonicalMesh]]:
    index: dict[str, dict[str, object]] = {}
    meshes: dict[str, CanonicalMesh] = {}
    for relative in REFERENCE_FILES:
        payload = _regular_payload(root / relative)
        try:
            canonical = parse_stl(payload, limits=StlLimits())
        except StlError as exc:
            raise ReferenceBundleError(f"invalid reference STL {relative}: {exc}") from exc
        index[relative] = {
            "bytes": len(payload),
            "sha256": _sha256(payload),
            "geometry_sha256": canonical.evidence.canonical_geometry_sha256,
        }
        meshes[relative] = canonical
    return index, meshes


def build_reference_bundle(
    root: Path,
    spec: ScenarioSpec,
    *,
    openfibsem_commit: str,
    evaluator_commit: str,
    generator_tree_sha256: str,
    reference_solution_sha256: str,
) -> ReferenceBundle:
    """Index pre-generated exact-scenario meshes and write a signed manifest."""

    root = Path(root).resolve()
    if not root.is_dir():
        raise ReferenceBundleError("reference bundle root is missing")
    openfibsem_commit = _validate_commit(openfibsem_commit, "OpenFIBSEM")
    evaluator_commit = _validate_commit(evaluator_commit, "evaluator")
    generator_tree_sha256 = _validate_sha256(generator_tree_sha256, "generator tree")
    reference_solution_sha256 = _validate_sha256(
        reference_solution_sha256, "reference solution"
    )
    files, _ = _mesh_index(root)
    parameters = _parameters()
    manifest: dict[str, object] = {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "source_id": "openfibsem",
        "evaluator_id": "fibsem_liftout_v1",
        "scenario": {
            "scenario_id": spec.scenario_id,
            "sha256": _sha256(spec.canonical_bytes()),
            "document": spec.to_dict(),
        },
        "implementation": {
            "openfibsem_commit": openfibsem_commit,
            "evaluator_commit": evaluator_commit,
            "generator_tree_sha256": generator_tree_sha256,
            "reference_solution_sha256": reference_solution_sha256,
        },
        "parameters": parameters,
        "parameter_sha256": _sha256(_canonical_json(parameters)),
        "files": files,
    }
    manifest["bundle_sha256"] = _sha256(_canonical_json(manifest))
    path = root / "reference-manifest.json"
    try:
        path.write_bytes(_canonical_json(manifest))
    except OSError as exc:
        raise ReferenceBundleError("cannot write reference manifest") from exc
    return load_reference_bundle(root, spec)


def _object(value: object, name: str, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ReferenceBundleError(f"reference {name} fields are invalid")
    return value


def load_reference_bundle(root: Path, spec: ScenarioSpec) -> ReferenceBundle:
    """Validate every indexed byte before exposing trusted reference meshes."""

    root = Path(root).resolve()
    manifest_path = root / "reference-manifest.json"
    payload = _regular_payload(manifest_path)
    try:
        manifest = json.loads(
            payload.decode("ascii"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite manifest number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReferenceBundleError("reference manifest is invalid JSON") from exc
    manifest = _object(
        manifest,
        "manifest",
        {
            "schema_version",
            "source_id",
            "evaluator_id",
            "scenario",
            "implementation",
            "parameters",
            "parameter_sha256",
            "files",
            "bundle_sha256",
        },
    )
    if manifest["schema_version"] != REFERENCE_SCHEMA_VERSION:
        raise ReferenceBundleError("reference schema version is incompatible")
    if manifest["source_id"] != "openfibsem" or (
        manifest["evaluator_id"] != "fibsem_liftout_v1"
    ):
        raise ReferenceBundleError("reference evaluator identity is incompatible")

    scenario = _object(
        manifest["scenario"], "scenario", {"scenario_id", "sha256", "document"}
    )
    scenario_digest = _validate_sha256(scenario["sha256"], "scenario")
    expected_scenario_digest = _sha256(spec.canonical_bytes())
    if (
        scenario["scenario_id"] != spec.scenario_id
        or scenario_digest != expected_scenario_digest
        or scenario["document"] != spec.to_dict()
    ):
        raise ReferenceBundleError("reference scenario identity does not match")

    implementation = _object(
        manifest["implementation"],
        "implementation",
        {
            "openfibsem_commit",
            "evaluator_commit",
            "generator_tree_sha256",
            "reference_solution_sha256",
        },
    )
    openfibsem_commit = _validate_commit(
        implementation["openfibsem_commit"], "OpenFIBSEM"
    )
    evaluator_commit = _validate_commit(
        implementation["evaluator_commit"], "evaluator"
    )
    generator_digest = _validate_sha256(
        implementation["generator_tree_sha256"], "generator tree"
    )
    solution_digest = _validate_sha256(
        implementation["reference_solution_sha256"], "reference solution"
    )
    parameters = manifest["parameters"]
    if parameters != _parameters():
        raise ReferenceBundleError("reference shape parameters are incompatible")
    parameter_digest = _validate_sha256(manifest["parameter_sha256"], "parameter")
    if parameter_digest != _sha256(_canonical_json(parameters)):
        raise ReferenceBundleError("reference parameter digest does not match")

    bundle_digest = _validate_sha256(manifest["bundle_sha256"], "bundle")
    digest_document = dict(manifest)
    del digest_document["bundle_sha256"]
    if bundle_digest != _sha256(_canonical_json(digest_document)):
        raise ReferenceBundleError("reference bundle digest does not match")

    file_index = manifest["files"]
    if not isinstance(file_index, Mapping) or set(file_index) != set(REFERENCE_FILES):
        raise ReferenceBundleError("reference file index is incomplete")
    meshes: dict[str, CanonicalMesh] = {}
    file_digests: dict[str, str] = {}
    for relative in REFERENCE_FILES:
        metadata = _object(
            file_index[relative],
            f"file {relative}",
            {"bytes", "sha256", "geometry_sha256"},
        )
        file_digest = _validate_sha256(metadata["sha256"], f"file {relative}")
        geometry_digest = _validate_sha256(
            metadata["geometry_sha256"], f"geometry {relative}"
        )
        file_payload = _regular_payload(root / relative)
        if metadata["bytes"] != len(file_payload) or file_digest != _sha256(file_payload):
            raise ReferenceBundleError(f"reference file digest mismatch: {relative}")
        try:
            mesh = parse_stl(file_payload)
        except StlError as exc:
            raise ReferenceBundleError(f"invalid reference STL {relative}: {exc}") from exc
        if mesh.evidence.canonical_geometry_sha256 != geometry_digest:
            raise ReferenceBundleError(f"reference geometry digest mismatch: {relative}")
        meshes[relative] = mesh
        file_digests[relative] = file_digest

    identity = ReferenceIdentity(
        schema_version=REFERENCE_SCHEMA_VERSION,
        source_id="openfibsem",
        evaluator_id="fibsem_liftout_v1",
        scenario_id=spec.scenario_id,
        scenario_sha256=scenario_digest,
        openfibsem_commit=openfibsem_commit,
        evaluator_commit=evaluator_commit,
        generator_tree_sha256=generator_digest,
        reference_solution_sha256=solution_digest,
        mesh_parser_version=MESH_PARSER_VERSION,
        algorithm_version=SHAPE_ALGORITHM_VERSION,
        parameter_sha256=parameter_digest,
        bundle_sha256=bundle_digest,
        file_sha256=MappingProxyType(dict(sorted(file_digests.items()))),
    )
    steps = MappingProxyType(
        {
            step: ReferenceStep(
                step_id=step,
                root=root / step,
                sample=meshes[f"{step}/sample.stl"],
                deposition=meshes[f"{step}/deposition.stl"],
            )
            for step in STEP_IDS
        }
    )
    document = scenario["document"]
    assert isinstance(document, Mapping)
    return ReferenceBundle(
        root=root,
        identity=identity,
        scenario_document=MappingProxyType(dict(document)),
        baseline_sample=meshes["baseline/sample.stl"],
        steps=steps,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or verify a FIBSEM reference bundle")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--root", type=Path, required=True)
        subparser.add_argument("--scenario", type=Path, required=True)
        if command == "build":
            subparser.add_argument("--openfibsem-commit", required=True)
            subparser.add_argument("--evaluator-commit", required=True)
            subparser.add_argument("--generator-tree-sha256", required=True)
            subparser.add_argument("--reference-solution-sha256", required=True)
    options = parser.parse_args(argv)
    spec = ScenarioSpec.from_path(options.scenario)
    if options.command == "build":
        build_reference_bundle(
            options.root,
            spec,
            openfibsem_commit=options.openfibsem_commit,
            evaluator_commit=options.evaluator_commit,
            generator_tree_sha256=options.generator_tree_sha256,
            reference_solution_sha256=options.reference_solution_sha256,
        )
    else:
        load_reference_bundle(options.root, spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
