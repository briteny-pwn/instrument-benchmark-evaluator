from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_default_pytest_collection_excludes_vendor_and_documents_isolated_gate() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = project["tool"]["pytest"]["ini_options"]

    assert options["testpaths"] == ["tests", "sources"]
    assert options["norecursedirs"] == ["vendor"]

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert (
        "vendor/pyvisa-sim-iab/pyvisa_sim/testsuite/test_all.py::"
        "test_multiple_outputs[ASRL5::INSTR]"
    ) in readme
