#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


EVALUATOR_ROOT = Path(__file__).resolve().parents[1]
if str(EVALUATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_ROOT))

from sources.openfibsem.fibsem_liftout_v1.backend import (  # noqa: E402
    OPENFIBSEM_COMMIT,
    OpenFibsemBackend,
)
from sources.openfibsem.fibsem_liftout_v1.checkpoint_exporter import (  # noqa: E402
    CheckpointExporter,
    _stl,
)
from sources.openfibsem.fibsem_liftout_v1.instrumented_microscope import (  # noqa: E402
    OperationDispatcher,
)
from sources.openfibsem.fibsem_liftout_v1.journal import EventJournal  # noqa: E402
from sources.openfibsem.fibsem_liftout_v1.reference_bundles import (  # noqa: E402
    STEP_IDS,
    build_reference_bundle,
)
from sources.openfibsem.fibsem_liftout_v1.scenario import (  # noqa: E402
    load_fixed_scenarios,
    seeded_scenarios,
)
from sources.openfibsem.fibsem_liftout_v1.service import FibsemService  # noqa: E402


class _CollectionRuntime:
    """Deterministic headless runtime for collecting trusted semantic meshes."""

    source_commit = OPENFIBSEM_COMMIT

    def ping(self) -> bool:
        return True

    def acquire_image(self, beam: str) -> tuple[int, int, bytes]:
        del beam
        width = height = 512
        row = bytes(index % 256 for index in range(width))
        return width, height, row * height

    def move_stage(self, position: tuple[float, float, float]) -> None:
        del position

    def move_manipulator(
        self, position: tuple[float, float, float], *, inserted: bool
    ) -> None:
        del position, inserted

    def run_pattern(
        self, operation: str, purpose: str, pattern: dict[str, object]
    ) -> None:
        del operation, purpose, pattern

    def stop(self, kind: str) -> None:
        del kind

    def synchronize(self, parts: tuple[object, ...]) -> None:
        del parts

    def close(self) -> None:
        pass


def _git_output(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _require_source_state(
    evaluator: Path,
    openfibsem: Path,
    *,
    allow_dirty: bool,
    evaluator_commit: str | None = None,
    openfibsem_commit: str | None = None,
) -> tuple[str, str]:
    if (evaluator_commit is None) != (openfibsem_commit is None):
        raise RuntimeError("both explicit provenance commits must be supplied")
    if evaluator_commit is not None and openfibsem_commit is not None:
        for name, value in (
            ("evaluator", evaluator_commit),
            ("OpenFIBSEM", openfibsem_commit),
        ):
            if len(value) != 40 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise RuntimeError(f"explicit {name} commit is invalid")
        if openfibsem_commit != OPENFIBSEM_COMMIT:
            raise RuntimeError("OpenFIBSEM checkout does not match the pinned commit")
        if not allow_dirty:
            raise RuntimeError(
                "explicit provenance is only allowed with --allow-dirty in a sealed image"
            )
        return evaluator_commit, openfibsem_commit
    openfibsem_commit = _git_output(openfibsem, "rev-parse", "HEAD")
    if openfibsem_commit != OPENFIBSEM_COMMIT:
        raise RuntimeError("OpenFIBSEM checkout does not match the pinned commit")
    evaluator_commit = _git_output(evaluator, "rev-parse", "HEAD")
    if not allow_dirty:
        for name, root in (("evaluator", evaluator), ("OpenFIBSEM", openfibsem)):
            dirty = _git_output(root, "status", "--porcelain", "--untracked-files=no")
            if dirty:
                raise RuntimeError(f"{name} tracked source tree is dirty")
    return evaluator_commit, openfibsem_commit


def _source_tree_digest(evaluator: Path) -> str:
    roots = (
        evaluator / "sources" / "openfibsem" / "fibsem_liftout_v1",
        evaluator / "scripts" / "generate_fibsem_reference_bundles.py",
    )
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and "reference_artifacts" not in path.parts
            and "__pycache__" not in path.parts
        )
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(evaluator).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "little"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "little"))
        digest.update(payload)
    return digest.hexdigest()


def _reference_module(path: Path):
    module_spec = importlib.util.spec_from_file_location(
        "fibsem_reference_bundle_solution", path
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("cannot load the FIBSEM reference solution")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _collect_world(spec, instance: Path, solution: Path, staging: Path) -> Path:
    from fibsem_iab import MicroscopeClient, Scenario
    from fibsem_iab.client import checkpoint_callback

    backend = OpenFibsemBackend(spec, runtime=_CollectionRuntime())
    baseline = backend.freeze_snapshot("step_1")
    baseline_sample = tuple(part for part in baseline.parts if part.role == "sample")
    if not baseline_sample:
        raise RuntimeError("reference baseline has no sample mesh")
    bundle_root = staging / spec.scenario_id
    (bundle_root / "baseline").mkdir(parents=True)
    (bundle_root / "baseline" / "sample.stl").write_bytes(_stl(baseline_sample))

    evidence_root = staging / f".{spec.scenario_id}-evidence"
    journal = EventJournal(f"reference-{spec.scenario_id}", spec.scenario_id)
    service = FibsemService(
        backend,
        spec,
        journal,
        CheckpointExporter(evidence_root),
    )
    dispatcher = OperationDispatcher(service, spec, journal)

    class DirectRpc:
        def call(self, operation, arguments=None):
            return dispatcher.dispatch(operation, dict(arguments or {}))

    microscope = MicroscopeClient(DirectRpc())
    result = _reference_module(solution).run_experiment(
        microscope,
        Scenario.from_dict(spec.to_dict()),
        checkpoint_callback(microscope),
        staging / f".{spec.scenario_id}-candidate-output",
    )
    service.finalize(outcome="completed", forced=False)
    if not isinstance(result, dict) or result.get("completed") is not True:
        raise RuntimeError(f"reference solution did not complete: {spec.scenario_id}")
    if tuple(service.frozen_checkpoints) != STEP_IDS:
        raise RuntimeError(f"reference checkpoints are incomplete: {spec.scenario_id}")
    for step in STEP_IDS:
        source = evidence_root / "artifacts" / spec.scenario_id / step / "components"
        destination = bundle_root / step
        destination.mkdir()
        for filename in ("sample.stl", "deposition.stl"):
            shutil.copyfile(source / filename, destination / filename)
    shutil.rmtree(evidence_root)
    candidate_output = staging / f".{spec.scenario_id}-candidate-output"
    if candidate_output.exists():
        shutil.rmtree(candidate_output)
    return bundle_root


def generate_all(
    *,
    instance: Path,
    openfibsem: Path,
    output: Path,
    allow_dirty: bool,
    evaluator_commit: str | None = None,
    openfibsem_commit: str | None = None,
) -> None:
    instance = instance.resolve()
    openfibsem = openfibsem.resolve()
    output = output.resolve()
    evaluator_commit, openfibsem_commit = _require_source_state(
        EVALUATOR_ROOT,
        openfibsem,
        allow_dirty=allow_dirty,
        evaluator_commit=evaluator_commit,
        openfibsem_commit=openfibsem_commit,
    )
    solution = (
        EVALUATOR_ROOT
        / "sources"
        / "openfibsem"
        / "fibsem_liftout_v1"
        / "reference"
        / "solution.py"
    )
    nominal = instance / "scenarios" / "nominal.json"
    specs = tuple(load_fixed_scenarios(nominal).values()) + seeded_scenarios(
        5,
        base_seed=47000,
        nominal_path=nominal,
    )
    generator_digest = _source_tree_digest(EVALUATOR_ROOT)
    solution_digest = hashlib.sha256(solution.read_bytes()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".fibsem-reference-", dir=output.parent)
    ).resolve()
    try:
        sys.path.insert(0, str(instance))
        try:
            for spec in specs:
                bundle = _collect_world(spec, instance, solution, staging)
                build_reference_bundle(
                    bundle,
                    spec,
                    openfibsem_commit=openfibsem_commit,
                    evaluator_commit=evaluator_commit,
                    generator_tree_sha256=generator_digest,
                    reference_solution_sha256=solution_digest,
                )
        finally:
            sys.path.remove(str(instance))
        backup = output.with_name(f".{output.name}.previous")
        if backup.exists():
            shutil.rmtree(backup)
        if output.exists():
            os.replace(output, backup)
        os.replace(staging, output)
        if backup.exists():
            shutil.rmtree(backup)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate all private FIBSEM references")
    parser.add_argument("--instance", type=Path, required=True)
    parser.add_argument("--openfibsem", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--evaluator-commit")
    parser.add_argument("--openfibsem-commit")
    options = parser.parse_args(argv)
    generate_all(
        instance=options.instance,
        openfibsem=options.openfibsem,
        output=options.output,
        allow_dirty=options.allow_dirty,
        evaluator_commit=options.evaluator_commit,
        openfibsem_commit=options.openfibsem_commit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
