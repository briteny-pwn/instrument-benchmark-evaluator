from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("relative_path", "source_id", "evaluator_id"),
    (
        (
            "tests/integration/test_v2_dual_container_linux.py",
            "pyvisa",
            "pyvisa_dut_validation_v2",
        ),
        (
            "tests/integration/test_fibsem_dual_container_linux.py",
            "openfibsem",
            "fibsem_liftout_v1",
        ),
        (
            "tests/integration/test_fibsem_full_suite_linux.py",
            "openfibsem",
            "fibsem_liftout_v1",
        ),
    ),
)
def test_native_linux_image_build_binds_composite_evaluator_identity(
    relative_path: str,
    source_id: str,
    evaluator_id: str,
) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative_path)
    builder_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "build"
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "cls"
        and node.func.value.attr == "builder"
    ]

    assert len(builder_calls) == 1
    keywords = {keyword.arg: keyword.value for keyword in builder_calls[0].keywords}
    assert "source_id" in keywords
    assert "evaluator_id" in keywords
    assert ast.literal_eval(keywords["source_id"]) == source_id
    assert ast.literal_eval(keywords["evaluator_id"]) == evaluator_id
