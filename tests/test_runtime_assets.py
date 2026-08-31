from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_evaluator_repository_owns_all_container_profiles() -> None:
    container = ROOT / "container"

    assert container.is_dir()
    assert {
        "evaluator.Dockerfile",
        "fibsem-evaluator.Dockerfile",
        "fibsem-validation-runner.Dockerfile",
        "fibsem-validation-runner.Dockerfile.dockerignore",
        "evaluator-requirements.lock",
        "openfibsem-requirements.lock",
        "wheelhouse",
        "openfibsem-wheelhouse",
        "fibsem-system-packages",
        "docker-cli",
        "docker-buildx",
    } <= {path.name for path in container.iterdir()}


def test_evaluator_repository_owns_container_vendoring_tools() -> None:
    scripts = ROOT / "scripts"

    assert {
        "vendor_docker_cli.py",
        "vendor_docker_buildx.py",
        "vendor_evaluator_wheels.py",
        "vendor_openfibsem_wheels.py",
    } <= {path.name for path in scripts.iterdir()}


def test_evaluator_ci_runs_owned_assets_and_acceptance_tests_with_instrument() -> None:
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "path: evaluator" in workflow
    assert "repository: briteny-pwn/instrument-benchmark-instances" in workflow
    assert "path: instance" in workflow
    assert "path: instrument" in workflow
    assert workflow.count("ref: distributed-model") == 2
    assert "python -m pip install -e . -e ../instrument pytest" in workflow
    assert "python -m pytest tests sources -q" in " ".join(workflow.split())
    assert (
        "instance/sources/pyvisa/pyvisa_dut_validation_v1" in workflow
    )
    assert (
        "instance/sources/pyvisa/pyvisa_dut_validation_v2" in workflow
    )
    assert "evaluators/pyvisa_dut_validation" not in workflow


def test_evaluator_integrations_never_load_assets_from_instrument() -> None:
    offenders = []
    for path in sorted((ROOT / "tests/integration").glob("test_*.py")):
        if 'INSTRUMENT / "container"' in path.read_text(encoding="utf-8"):
            offenders.append(path.name)

    assert offenders == []
