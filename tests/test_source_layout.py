from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def discover_evaluators(root: Path) -> list[tuple[str, str, dict[str, Any]]]:
    schema = json.loads((root / "schemas/source.schema.json").read_text())
    if (root / "evaluator.yaml").exists() or (root / "evaluators").exists():
        raise ValueError("legacy evaluator layout is forbidden")
    records: list[tuple[str, str, dict[str, Any]]] = []
    for source_root in sorted(
        path
        for path in (root / "sources").iterdir()
        if path.is_dir() and path.name != "__pycache__"
    ):
        if source_root.is_symlink() or not ID_PATTERN.fullmatch(source_root.name):
            raise ValueError("invalid source directory")
        source = yaml.safe_load((source_root / "source.yaml").read_text())
        jsonschema.Draft202012Validator(schema).validate(source)
        if source["source_id"] != source_root.name:
            raise ValueError("source identity mismatch")
        registered = source["evaluators"]
        if registered != sorted(set(registered)):
            raise ValueError("evaluator registry must be unique and sorted")
        actual = sorted(
            path.name
            for path in source_root.iterdir()
            if path.is_dir() and (path / "evaluator.yaml").is_file()
        )
        if registered != actual:
            raise ValueError("evaluator registry and leaves differ")
        for evaluator_id in registered:
            leaf = source_root / evaluator_id
            if leaf.is_symlink() or not ID_PATTERN.fullmatch(evaluator_id):
                raise ValueError("invalid evaluator leaf")
            manifest = yaml.safe_load((leaf / "evaluator.yaml").read_text())
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema_version") != 2
                or manifest.get("source_id") != source["source_id"]
                or manifest.get("evaluator_id") != evaluator_id
                or manifest.get("protocol_version") != 2
            ):
                raise ValueError("evaluator manifest identity mismatch")
            supported = manifest.get("supported_instances")
            if not isinstance(supported, list) or supported != sorted(set(supported)):
                raise ValueError("supported instances must be unique and sorted")
            records.append((source["source_id"], evaluator_id, manifest))
    return records


def copied_root(tmp_path: Path) -> Path:
    root = tmp_path / "evaluator"
    (root / "schemas").mkdir(parents=True)
    shutil.copy2(
        ROOT / "schemas" / "source.schema.json",
        root / "schemas" / "source.schema.json",
    )
    shutil.copytree(ROOT / "sources", root / "sources", symlinks=True)
    return root


def test_discovers_registered_evaluators_and_forbids_legacy_layout() -> None:
    records = discover_evaluators(ROOT)
    discovered_ids = [(source_id, evaluator_id) for source_id, evaluator_id, _ in records]

    assert discovered_ids == [
        ("openfibsem", "fibsem_liftout_v1"),
        ("pyvisa", "pyvisa_dut_validation_v1"),
        ("pyvisa", "pyvisa_dut_validation_v2"),
    ]
    assert not (ROOT / "evaluators").exists()
    assert not (ROOT / "evaluator.yaml").exists()
    assert not (ROOT / "instrument_benchmark_evaluator/evaluator.yaml").exists()
    for source_id, evaluator_id, manifest in records:
        assert manifest["schema_version"] == 2
        assert manifest["source_id"] == source_id
        assert manifest["evaluator_id"] == evaluator_id
        assert manifest["protocol_version"] == 2
        assert manifest["supported_instances"] == sorted(
            set(manifest["supported_instances"])
        )


def test_discovery_rejects_unregistered_leaf(tmp_path: Path) -> None:
    root = copied_root(tmp_path)
    pyvisa = root / "sources" / "pyvisa"
    shutil.copytree(pyvisa / "pyvisa_dut_validation_v1", pyvisa / "unregistered")

    with pytest.raises(ValueError, match="registry and leaves differ"):
        discover_evaluators(root)


def test_discovery_rejects_orphan_registry_entry(tmp_path: Path) -> None:
    root = copied_root(tmp_path)
    registry = root / "sources" / "pyvisa" / "source.yaml"
    source = yaml.safe_load(registry.read_text())
    source["evaluators"].append("zz_orphan")
    registry.write_text(yaml.safe_dump(source, sort_keys=False))

    with pytest.raises(ValueError, match="registry and leaves differ"):
        discover_evaluators(root)


def test_discovery_rejects_root_flat_manifest(tmp_path: Path) -> None:
    root = copied_root(tmp_path)
    (root / "evaluator.yaml").write_text("{}\n")

    with pytest.raises(ValueError, match="legacy evaluator layout is forbidden"):
        discover_evaluators(root)


def test_discovery_rejects_symlink_source(tmp_path: Path) -> None:
    root = copied_root(tmp_path)
    sources = root / "sources"
    (sources / "linked").symlink_to(sources / "pyvisa", target_is_directory=True)

    with pytest.raises(ValueError, match="invalid source directory"):
        discover_evaluators(root)


def test_discovery_rejects_symlink_leaf(tmp_path: Path) -> None:
    root = copied_root(tmp_path)
    pyvisa = root / "sources" / "pyvisa"
    leaf = pyvisa / "pyvisa_dut_validation_v2"
    real_leaf = tmp_path / "real_leaf"
    leaf.rename(real_leaf)
    leaf.symlink_to(real_leaf, target_is_directory=True)

    with pytest.raises(ValueError, match="invalid evaluator leaf"):
        discover_evaluators(root)


def test_discovery_rejects_mismatched_source_id(tmp_path: Path) -> None:
    root = copied_root(tmp_path)
    manifest_path = (
        root
        / "sources"
        / "openfibsem"
        / "fibsem_liftout_v1"
        / "evaluator.yaml"
    )
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["source_id"] = "pyvisa"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

    with pytest.raises(ValueError, match="evaluator manifest identity mismatch"):
        discover_evaluators(root)
