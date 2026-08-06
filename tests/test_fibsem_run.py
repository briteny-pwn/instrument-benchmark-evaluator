from __future__ import annotations

from pathlib import Path
import json
from unittest.mock import patch

from sources.openfibsem.fibsem_liftout_v1.scenario import canonical_document
from instrument_benchmark_evaluator import bootstrap
from instrument_benchmark_evaluator.container.bootstrap_contract import BootstrapPaths
from instrument_benchmark_evaluator.contracts import RunSettings, load_instance_settings
from instrument_benchmark_evaluator.dispatch import resolve_evaluator_target
from instrument_benchmark_evaluator.fibsem_run import (
    FibsemWorldExecution,
    _make_host_cleanup_directories,
    fibsem_suite_specs,
    run_fibsem_full_suite,
)
from instrument_benchmark_evaluator.cli import main
from sources.openfibsem.fibsem_liftout_v1.tests.test_scoring import world_report


ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT.parent / "instance" / "sources" / "openfibsem" / "fibsem_liftout_v1"


def test_composite_dispatch_preserves_all_evaluator_kinds() -> None:
    assert resolve_evaluator_target(
        "openfibsem", "fibsem_liftout_v1", "fibsem_liftout_v1"
    ).kind == "fibsem"
    assert resolve_evaluator_target(
        "pyvisa", "pyvisa_dut_validation_v2", "pyvisa_dut_validation_v2"
    ).kind == "pyvisa_v2"
    assert resolve_evaluator_target(
        "pyvisa", "pyvisa_dut_validation_v1", "pyvisa_dut_validation_v1"
    ).kind == "pyvisa_v1"


def test_fibsem_suite_is_exactly_five_fixed_then_five_seeded() -> None:
    specs = fibsem_suite_specs(INSTANCE, repeated_base_seed=47000)

    assert [spec.scenario_id for spec in specs] == [
        "nominal",
        "small",
        "large",
        "needle_offset",
        "target_pose",
        "seeded_01",
        "seeded_02",
        "seeded_03",
        "seeded_04",
        "seeded_05",
    ]
    assert [spec.seed for spec in specs[5:]] == list(range(47000, 47005))


def test_fibsem_seeded_suite_is_byte_deterministic() -> None:
    first = fibsem_suite_specs(INSTANCE, repeated_base_seed=47000)
    second = fibsem_suite_specs(INSTANCE, repeated_base_seed=47000)

    assert [canonical_document(spec.to_dict()) for spec in first] == [
        canonical_document(spec.to_dict()) for spec in second
    ]


def test_completed_world_tree_directories_are_host_cleanable(tmp_path: Path) -> None:
    nested = tmp_path / "workspace" / "docs"
    nested.mkdir(parents=True)
    tmp_path.chmod(0o755)
    (tmp_path / "workspace").chmod(0o755)
    nested.chmod(0o755)

    _make_host_cleanup_directories(tmp_path)

    assert tmp_path.stat().st_mode & 0o777 == 0o777
    assert (tmp_path / "workspace").stat().st_mode & 0o777 == 0o777
    assert nested.stat().st_mode & 0o777 == 0o777


def test_fibsem_bootstrap_calls_exact_four_argument_entrypoint(tmp_path: Path) -> None:
    paths = BootstrapPaths(
        solution=tmp_path / "solution.py",
        endpoint=tmp_path / "fibsem.sock",
        output=tmp_path / "output" / "result.json",
        returned=tmp_path / "output" / "return.json",
        workspace=tmp_path,
        output_root=tmp_path / "output",
    )
    microscope, scenario, checkpoint = object(), object(), object()
    calls: list[tuple[object, ...]] = []

    def entrypoint(*arguments: object) -> dict[str, object]:
        calls.append(arguments)
        return {"completed": True}

    result = bootstrap._invoke_fibsem_entrypoint(
        entrypoint,
        paths,
        runtime_factory=lambda supplied: (
            microscope,
            scenario,
            checkpoint,
        ),
    )

    assert result == {"completed": True}
    assert calls == [(microscope, scenario, checkpoint, paths.output_root)]


def test_cli_dispatches_fibsem_with_exact_evaluator_image(tmp_path: Path) -> None:
    candidate = (
        ROOT
        / "sources"
        / "openfibsem"
        / "fibsem_liftout_v1"
        / "reference"
        / "solution.py"
    )
    shared = tmp_path / "shared"
    shared.mkdir()
    image_id = "sha256:" + "c" * 64
    request = {
        "protocol_version": 2,
        "run_id": "fibsem-test",
        "source_id": "openfibsem",
        "instance_id": "fibsem_liftout_v1",
        "evaluator_id": "fibsem_liftout_v1",
        "instance_path": str(INSTANCE.resolve()),
        "candidate_path": str(candidate.resolve()),
        "timeout_seconds": 180,
        "max_output_bytes": 1048576,
        "repeated_worlds": 5,
        "repeated_base_seed": 47000,
        "container_protocol_version": 1,
        "image_mode": "locked",
        "shared_run_root": str(shared.resolve()),
        "evaluator_image_id": image_id,
    }
    request_path = tmp_path / "request.json"
    report_path = tmp_path / "report.json"
    request_path.write_text(json.dumps(request))

    class Report:
        def to_dict(self):
            return {
                "schema_version": 4,
                "source_id": "openfibsem",
                "evaluator_id": "fibsem_liftout_v1",
            }

    backend, sim_runner = object(), object()
    with patch(
        "instrument_benchmark_evaluator.fibsem_run.run_fibsem_full_suite",
        return_value=Report(),
    ) as run:
        status = main(
            ["run", "--request", str(request_path), "--report", str(report_path)],
            backend_factory=lambda instance: backend,
            sim_runner_factory=lambda supplied: (
                supplied == image_id and sim_runner
            ),
        )

    assert status == 0
    assert run.call_args.kwargs["backend"] is backend
    assert run.call_args.kwargs["sim_runner"] is sim_runner
    written = json.loads(report_path.read_text())
    assert written == {
        "schema_version": 4,
        "source_id": "openfibsem",
        "evaluator_id": "fibsem_liftout_v1",
    }


def test_full_suite_executes_each_world_once_in_declared_order(tmp_path: Path) -> None:
    benchmark = RunSettings(
        instance_path=INSTANCE,
        fixed_worlds=("nominal", "small", "large", "needle_offset", "target_pose"),
        repeated_worlds=5,
        timeout_seconds=180,
        max_output_bytes=1048576,
        shared_run_root=tmp_path,
    )
    seen: list[str] = []

    def execute(**kwargs):
        world_id = kwargs["spec"].scenario_id
        seen.append(world_id)
        return FibsemWorldExecution(None, None, world_report(world_id), None)

    with patch(
        "instrument_benchmark_evaluator.fibsem_run.run_fibsem_world",
        side_effect=execute,
    ):
        report = run_fibsem_full_suite(
            benchmark=benchmark,
            instance=load_instance_settings(
                INSTANCE,
                expected_source_id="openfibsem",
                expected_instance_id="fibsem_liftout_v1",
                expected_evaluator_id="fibsem_liftout_v1",
            ),
            candidate_path=ROOT / "sources/openfibsem/fibsem_liftout_v1/reference/solution.py",
            backend=object(),
            sim_runner=object(),
            repeated_base_seed=47000,
        )

    assert seen == [
        "nominal",
        "small",
        "large",
        "needle_offset",
        "target_pose",
        "seeded_01",
        "seeded_02",
        "seeded_03",
        "seeded_04",
        "seeded_05",
    ]
    assert report.strict_pass


def test_serve_fibsem_sim_dispatches_absolute_isolated_paths(tmp_path: Path) -> None:
    world = tmp_path / "world.json"
    endpoint = tmp_path / "transport" / "fibsem.sock"
    evidence = tmp_path / "evidence"
    with patch(
        "instrument_benchmark_evaluator.cli._serve_fibsem_sim",
        return_value=19,
    ) as serve:
        status = main(
            [
                "serve-fibsem-sim",
                "--world",
                str(world),
                "--endpoint",
                str(endpoint),
                "--evidence",
                str(evidence),
                "--run-id",
                "run-19",
            ]
        )

    assert status == 19
    serve.assert_called_once_with(
        world=world.resolve(),
        endpoint=endpoint.resolve(),
        evidence=evidence.resolve(),
        run_id="run-19",
    )
