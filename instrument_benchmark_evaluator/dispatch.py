from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from .contracts import ContractError


EvaluatorKind = Literal["pyvisa_v1", "pyvisa_v2", "fibsem"]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TARGETS: dict[tuple[str, str], EvaluatorKind] = {
    ("pyvisa", "pyvisa_dut_validation_v1"): "pyvisa_v1",
    ("pyvisa", "pyvisa_dut_validation_v2"): "pyvisa_v2",
    ("openfibsem", "fibsem_liftout_v1"): "fibsem",
}


@dataclass(frozen=True)
class EvaluatorTarget:
    source_id: str
    evaluator_id: str
    instance_id: str
    kind: EvaluatorKind
    root: Path
    manifest: dict[str, Any]


def resolve_evaluator_target(
    source_id: str, evaluator_id: str, instance_id: str
) -> EvaluatorTarget:
    try:
        kind = TARGETS[(source_id, evaluator_id)]
    except KeyError as exc:
        raise ContractError(
            "unsupported source/evaluator/instance combination"
        ) from exc

    source_root = PACKAGE_ROOT / "sources" / source_id
    source_manifest_path = source_root / "source.yaml"
    if source_root.is_symlink() or not source_manifest_path.is_file():
        raise ContractError("packaged evaluator source is missing")
    source = _load_yaml(source_manifest_path, "packaged evaluator source manifest")
    evaluators = source.get("evaluators") if isinstance(source, dict) else None
    if (
        not isinstance(source, dict)
        or source.get("schema_version") != 1
        or source.get("source_id") != source_id
        or not isinstance(evaluators, list)
        or evaluator_id not in evaluators
    ):
        raise ContractError("unsupported source/evaluator/instance combination")

    root = source_root / evaluator_id
    manifest_path = root / "evaluator.yaml"
    if root.is_symlink() or not manifest_path.is_file():
        raise ContractError("packaged evaluator manifest is missing")
    manifest = _load_yaml(manifest_path, "packaged evaluator manifest")
    supported_instances = (
        manifest.get("supported_instances") if isinstance(manifest, dict) else None
    )
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 2
        or manifest.get("source_id") != source_id
        or manifest.get("evaluator_id") != evaluator_id
        or manifest.get("protocol_version") != 2
        or not isinstance(supported_instances, list)
        or instance_id not in supported_instances
    ):
        raise ContractError("unsupported source/evaluator/instance combination")
    return EvaluatorTarget(source_id, evaluator_id, instance_id, kind, root, manifest)


def _load_yaml(path: Path, name: str) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError(f"{name} is invalid") from exc
