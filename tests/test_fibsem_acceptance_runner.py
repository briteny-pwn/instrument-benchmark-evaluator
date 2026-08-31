from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts.validate_fibsem_benchmark import (
    OPENFIBSEM_COMMIT,
    ValidationError,
    main,
    validate_distributed_report,
)
from instrument_benchmark.environment import RepositoryPaths


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT_ROOT = ROOT.parent / "instrument"
DOCKERFILE = ROOT / "container" / "fibsem-validation-runner.Dockerfile"
DOCKERIGNORE = ROOT / "container" / "fibsem-validation-runner.Dockerfile.dockerignore"
RUNNER = ROOT / "scripts" / "run_fibsem_linux_acceptance.sh"


def test_validation_runner_has_a_pinned_python_and_bounded_tools() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    wheel = (
        "pyyaml-6.0.3-cp311-cp311-manylinux2014_x86_64."
        "manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    )

    assert (
        "FROM python:3.11.9-slim-bookworm@sha256:"
        "2856e6af199e8128161abd320575eb9b341f3b76f017b5d0c9cd364f60d8a050"
        in text
    )
    assert "apt-get" not in text
    assert "COPY git" not in text
    assert "COPY wheelhouse/pyyaml-6.0.3-" in text
    assert "COPY wheelhouse/python_dotenv-1.2.3-py3-none-any.whl" in text
    assert "904552145e8bfed22162c09dab1c2b9b54fefa7b23ba780f4f26ca0316b0f0d9" in text
    assert f"/build/{wheel}" in text
    assert "/build/pyyaml.whl" not in text
    assert "python -m pip install --no-index" in text
    assert "COPY docker-cli/docker /usr/local/bin/docker" in text
    assert "242c7a8de606afba2acada7c7af00d77f92c3601678b2f3a60911b49a892c722" in text
    assert 'ENTRYPOINT ["python"]' in text

    ignored = DOCKERIGNORE.read_text(encoding="utf-8")
    assert ignored.splitlines()[0] == "**"
    assert "!docker-cli/docker" in ignored
    assert "!wheelhouse/pyyaml-6.0.3-" in ignored
    assert "!wheelhouse/python_dotenv-1.2.3-py3-none-any.whl" in ignored
    assert "!openfibsem-wheelhouse" not in ignored


def test_native_linux_runner_preserves_daemon_visible_paths_and_identity() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    normalized = " ".join(text.replace("\\\n", "").split())

    assert 'test "$(uname -s)" = "Linux"' in text
    assert 'test "$(uname -m)" = "x86_64"' in text
    assert 'test "$#" -eq 1' in text
    assert 'evaluator_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)' in text
    assert (
        'instrument_root=$(git -C "$(dirname -- "$config_path")" '
        'rev-parse --show-toplevel)' in text
    )
    assert "docker build" in text
    assert "--platform linux/amd64" in text
    assert "--network=none" in text
    assert "git_bin=$(command -v git)" in text
    assert "git_exec_path=$(git --exec-path)" in text
    assert 'ldd "$git_bin"' in text
    assert '$3 !~ /\\/libc\\.so\\./' in text
    assert '$1 !~ /\\/ld-linux/' in text
    assert 'src="$git_bin",dst="$git_bin",readonly' in text
    assert 'src="$git_exec_path",dst="$git_exec_path",readonly' in text
    assert '--user "$(id -u):$(id -g)"' in text
    assert '--group-add "$socket_gid"' in text
    assert "src=/var/run/docker.sock,dst=/var/run/docker.sock" in text
    assert 'src=/tmp,dst=/tmp' in text
    assert "read_repository_path_values" in text
    assert 'src="$instrument_root",dst="$instrument_root"' in text
    assert 'src="$instances_repo_path",dst="$instances_repo_path"' in text
    assert 'src="$evaluator_repo_path",dst="$evaluator_repo_path"' in text
    assert 'test "$evaluator_repo_path" = "$evaluator_root"' in text
    assert '--env "INSTANCES_REPO_PATH=$instances_repo_path"' in text
    assert '--env "EVALUATOR_REPO_PATH=$evaluator_repo_path"' in text
    assert 'test -d "$checkout_parent/instance/.git"' not in text
    assert 'test -d "$checkout_parent/evaluator/.git"' not in text
    assert 'test -d "$instances_repo_path/.git"' not in text
    assert 'test -d "$evaluator_repo_path/.git"' not in text
    assert 'test -d "$checkout_parent/fibsem/.git"' not in text
    assert 'git -C "$instances_repo_path" rev-parse --show-toplevel' in text
    assert 'git -C "$evaluator_repo_path" rev-parse --show-toplevel' in text
    assert 'git -C "$checkout_parent/fibsem" rev-parse --show-toplevel' in text
    assert 'python scripts/validate_fibsem_benchmark.py' not in text
    assert (
        '"$evaluator_root/scripts/validate_fibsem_benchmark.py" '
        '--instrument-root "$instrument_root" --config "$config_path"'
        in normalized
    )
    assert "config_arg=$1" in text


def test_fibsem_validator_defaults_to_source_grouped_config_and_rejects_cross_source(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        source_id="pyvisa",
        evaluator_id="fibsem_liftout_v1",
        report_path=tmp_path / "report.json",
    )
    repositories = RepositoryPaths(Path("/instances"), Path("/evaluator"))
    with (
        mock.patch(
            "scripts.validate_fibsem_benchmark.load_repository_paths",
            return_value=repositories,
        ),
        mock.patch(
            "scripts.validate_fibsem_benchmark.load_run_config",
            return_value=config,
        ) as loader,
    ):
        with pytest.raises(ValidationError, match="source.*evaluator|identity"):
            main(
                [
                    "--instrument-root",
                    str(INSTRUMENT_ROOT),
                    "--config",
                    str(
                        INSTRUMENT_ROOT
                        / "configs/openfibsem/fibsem_liftout_v1.yaml"
                    ),
                ]
            )

    loader.assert_called_once_with(
        (INSTRUMENT_ROOT / "configs/openfibsem/fibsem_liftout_v1.yaml").resolve(),
        repositories,
    )


def test_fibsem_validator_requires_source_aware_report_v5(tmp_path: Path) -> None:
    report = {
        "schema_version": 5,
        "source_id": "openfibsem",
        "evaluator_id": "fibsem_liftout_v1",
        "openfibsem_commit": OPENFIBSEM_COMMIT,
        "score": 100.0,
        "strict_pass": True,
        "retry_eligible": False,
        "evidence_confidence": 1.0,
    }

    with pytest.raises(ValidationError, match="provenance"):
        validate_distributed_report(report, report_path=tmp_path / "report.json")

    report["schema_version"] = 4
    with pytest.raises(ValidationError, match="strict score-100"):
        validate_distributed_report(report, report_path=tmp_path / "report.json")

    report["schema_version"] = 5
    report["source_id"] = "pyvisa"
    with pytest.raises(ValidationError, match="strict score-100"):
        validate_distributed_report(report, report_path=tmp_path / "report.json")


def test_readme_publishes_the_portable_native_linux_entrypoint() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "scripts/run_fibsem_linux_acceptance.sh" in text
    assert "Python 3.11" in text
    assert "identical absolute path" in text
