from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]

from instrument_benchmark.environment import RepositoryPaths  # noqa: E402
from scripts import validate_fibsem_benchmark as validator  # noqa: E402


def test_fibsem_validator_loads_repository_paths_once(tmp_path: Path) -> None:
    class StopAfterConfig(RuntimeError):
        pass

    repositories = RepositoryPaths(
        instances_repo_path=tmp_path / "instances",
        evaluator_repo_path=tmp_path / "evaluator",
    )
    (tmp_path / "src/instrument_benchmark").mkdir(parents=True)
    config_path = tmp_path / "configs/openfibsem/fibsem_liftout_v1.yaml"
    with (
        patch.object(
            validator,
            "load_repository_paths",
            return_value=repositories,
        ) as environment_loader,
        patch.object(
            validator,
            "load_run_config",
            side_effect=StopAfterConfig,
        ) as config_loader,
    ):
        try:
            validator.main(
                [
                    "--instrument-root",
                    str(tmp_path),
                    "--config",
                    str(config_path),
                ]
            )
        except StopAfterConfig:
            pass
        else:
            raise AssertionError("validator did not reach config loading")

    environment_loader.assert_called_once_with(tmp_path)
    config_loader.assert_called_once_with(
        config_path.resolve(),
        repositories,
    )


def test_repeat_config_keeps_repository_paths_out_of_yaml(tmp_path: Path) -> None:
    source = tmp_path / "source.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "run_id": "fibsem-reference",
                "source_id": "openfibsem",
                "instance_id": "fibsem_liftout_v1",
                "evaluator_id": "fibsem_liftout_v1",
                "candidate_path": (
                    "sources/openfibsem/fibsem_liftout_v1/reference/solution.py"
                ),
                "report_path": "report.json",
                "timeout_seconds": 180,
                "max_output_bytes": 1_048_576,
                "repeated_worlds": 5,
                "repeated_base_seed": 47_000,
                "container_protocol_version": 1,
                "image_mode": "locked",
                "openfibsem_checkout": "../fibsem",
                "openfibsem_commit": "a" * 40,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "repeat.yaml"
    report = tmp_path / "repeat.json"
    repositories = RepositoryPaths(
        instances_repo_path=tmp_path / "instances",
        evaluator_repo_path=tmp_path / "evaluator",
    )
    loaded = SimpleNamespace(
        run_id="fibsem-reference",
        candidate_path=tmp_path / "evaluator/solution.py",
        openfibsem_checkout=tmp_path / "fibsem",
    )

    with patch.object(
        validator,
        "load_run_config",
        return_value=loaded,
    ) as config_loader:
        validator._repeat_config(source, destination, report, repositories)

    config_loader.assert_called_once_with(source, repositories)
    value = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert value["schema_version"] == 3
    assert value["run_id"] == "fibsem-reference-repeat"
    assert value["report_path"] == str(report)
    assert "instance_checkout" not in value
    assert "evaluator_checkout" not in value
