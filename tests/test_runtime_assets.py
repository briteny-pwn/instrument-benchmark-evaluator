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
