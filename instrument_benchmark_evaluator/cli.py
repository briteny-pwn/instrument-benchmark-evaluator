from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

import yaml

from . import PROTOCOL_VERSION
from .contracts import (
    ContractError,
    RunSettings,
    load_evaluator_request,
    load_instance_settings,
)
from .candidate_backend import CandidateBackend, DockerCandidateBackend
from .container.docker_client import DockerClient
from .run import run_full_suite
from evaluators.pyvisa_dut_validation_v1 import worlds as world_resources


MANIFEST = Path(__file__).with_name("evaluator.yaml")
V2_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "evaluators"
    / "pyvisa_dut_validation_v2"
    / "evaluator.yaml"
)
WORLD_DIRECTORY = Path(world_resources.__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="instrument-evaluator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--report", type=Path, required=True)
    serve = subparsers.add_parser("serve-sim")
    serve.add_argument("--world", type=Path, required=True)
    serve.add_argument("--endpoint", type=Path, required=True)
    serve.add_argument("--evidence", type=Path, required=True)
    serve.add_argument("--simulator", type=Path)
    serve.add_argument("--run-id", required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    backend_factory: Callable[[object], CandidateBackend] | None = None,
    sim_runner_factory: Callable[[str], object] | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "serve-sim":
        service_arguments = [
            "--world",
            str(arguments.world.resolve()),
            "--endpoint",
            str(arguments.endpoint.resolve()),
            "--evidence",
            str(arguments.evidence.resolve()),
        ]
        if arguments.simulator is not None:
            service_arguments.extend(
                ["--simulator", str(arguments.simulator.resolve())]
            )
        service_arguments.extend(["--run-id", arguments.run_id])
        return _serve_sim(service_arguments)
    if arguments.command != "run":
        return 2
    try:
        request = load_evaluator_request(arguments.request.resolve())
        instance = load_instance_settings(
            request.instance_path, expected_evaluator_id=request.instance_id
        )
        manifest_path = (
            V2_MANIFEST
            if request.instance_id == "pyvisa_dut_validation_v2"
            else MANIFEST
        )
        manifest = yaml.safe_load(
            manifest_path.read_text(encoding="utf-8")
        )
        settings = RunSettings(
            instance_path=request.instance_path,
            fixed_worlds=tuple(manifest["fixed_worlds"]),
            repeated_worlds=request.repeated_worlds,
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
            run_id=request.run_id,
            shared_run_root=request.shared_run_root,
        )
        docker: DockerClient | None = None
        if backend_factory is not None:
            backend = backend_factory(instance)
        else:
            docker = DockerClient()
            backend = DockerCandidateBackend.from_instance(
                instance,
                client=docker,
                shared_run_root=request.shared_run_root,
            )
        if request.instance_id == "pyvisa_dut_validation_v2":
            from evaluators.pyvisa_dut_validation_v1.worlds import (
                load_world_specs,
            )

            from .v2_run import run_v2_full_suite

            assert request.evaluator_image_id is not None
            if sim_runner_factory is not None:
                sim_runner = sim_runner_factory(request.evaluator_image_id)
            else:
                from .container.sim_runner import SimContainerRunner

                docker = docker or DockerClient()
                sim_runner = SimContainerRunner(
                    client=docker,
                    evaluator_image_id=request.evaluator_image_id,
                )
            report = run_v2_full_suite(
                benchmark=settings,
                instance=instance,
                specs=load_world_specs(WORLD_DIRECTORY),
                candidate_path=request.candidate_path,
                backend=backend,
                sim_runner=sim_runner,
                repeated_base_seed=request.repeated_base_seed,
            ).to_dict()
        else:
            report = run_full_suite(
                benchmark=settings,
                instance=instance,
                candidate_path=request.candidate_path,
                world_directory=WORLD_DIRECTORY,
                repeated_base_seed=request.repeated_base_seed,
                backend=backend,
            ).to_dict()
        report["evaluator"] = {
            "id": request.instance_id,
            "protocol_version": PROTOCOL_VERSION,
            "run_id": request.run_id,
        }
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    except ContractError as exc:
        print(f"invalid evaluator request: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"evaluator infrastructure failure: {exc}", file=sys.stderr)
        return 3


def _serve_sim(argv: list[str]) -> int:
    from evaluators.pyvisa_dut_validation_v2.service import main as service_main

    return service_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
