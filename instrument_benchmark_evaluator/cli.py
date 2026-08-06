from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Callable

from .contracts import (
    ContractError,
    RunSettings,
    load_evaluator_request,
    load_instance_settings,
)
from .dispatch import resolve_evaluator_target
from .candidate_backend import CandidateBackend, DockerCandidateBackend
from .container.docker_client import DockerClient


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
    fibsem = subparsers.add_parser("serve-fibsem-sim")
    fibsem.add_argument("--world", type=Path, required=True)
    fibsem.add_argument("--endpoint", type=Path, required=True)
    fibsem.add_argument("--evidence", type=Path, required=True)
    fibsem.add_argument("--run-id", required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    backend_factory: Callable[[object], CandidateBackend] | None = None,
    sim_runner_factory: Callable[[str], object] | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "serve-fibsem-sim":
        return _serve_fibsem_sim(
            world=arguments.world.resolve(),
            endpoint=arguments.endpoint.resolve(),
            evidence=arguments.evidence.resolve(),
            run_id=arguments.run_id,
        )
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
        target = resolve_evaluator_target(
            request.source_id, request.evaluator_id, request.instance_id
        )
        instance = load_instance_settings(
            request.instance_path,
            expected_source_id=request.source_id,
            expected_instance_id=request.instance_id,
            expected_evaluator_id=request.evaluator_id,
        )
        kind = target.kind
        manifest = target.manifest
        fixed_worlds = tuple(manifest["fixed_worlds"])
        settings = RunSettings(
            instance_path=request.instance_path,
            fixed_worlds=fixed_worlds,
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
        if kind == "pyvisa_v2":
            from sources.pyvisa.pyvisa_dut_validation_v1 import worlds as world_resources
            from sources.pyvisa.pyvisa_dut_validation_v1.worlds import (
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
                specs=load_world_specs(Path(world_resources.__file__).resolve().parent),
                candidate_path=request.candidate_path,
                backend=backend,
                sim_runner=sim_runner,
                repeated_base_seed=request.repeated_base_seed,
            ).to_dict()
        elif kind == "fibsem":
            from .container.fibsem_sim_runner import FibsemSimContainerRunner
            from .fibsem_run import run_fibsem_full_suite

            assert request.evaluator_image_id is not None
            if sim_runner_factory is not None:
                sim_runner = sim_runner_factory(request.evaluator_image_id)
            else:
                docker = docker or DockerClient()
                sim_runner = FibsemSimContainerRunner(
                    client=docker,
                    evaluator_image_id=request.evaluator_image_id,
                )
            report = run_fibsem_full_suite(
                benchmark=settings,
                instance=instance,
                candidate_path=request.candidate_path,
                backend=backend,
                sim_runner=sim_runner,
                repeated_base_seed=request.repeated_base_seed,
            ).to_dict()
        else:
            from sources.pyvisa.pyvisa_dut_validation_v1 import worlds as world_resources

            from .run import run_full_suite

            report = run_full_suite(
                benchmark=settings,
                instance=instance,
                candidate_path=request.candidate_path,
                world_directory=Path(world_resources.__file__).resolve().parent,
                repeated_base_seed=request.repeated_base_seed,
                backend=backend,
            ).to_dict()
        if kind != "fibsem":
            if report.get("source_id") != request.source_id:
                raise ContractError("evaluator report source_id does not match request")
            report["evaluator"] = {
                "source_id": request.source_id,
                "id": request.evaluator_id,
                "protocol_version": 2,
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
    from sources.pyvisa.pyvisa_dut_validation_v2.service import main as service_main

    return service_main(argv)


def _serve_fibsem_sim(
    *, world: Path, endpoint: Path, evidence: Path, run_id: str
) -> int:
    from sources.openfibsem.fibsem_liftout_v1.service import (
        ServiceStopRequested,
        run_service,
    )

    previous = signal.getsignal(signal.SIGTERM)

    def stop(_signum, _frame):
        raise ServiceStopRequested("outer evaluator requested finalization")

    signal.signal(signal.SIGTERM, stop)
    try:
        run_service(world, endpoint, evidence, run_id)
        return 0
    except Exception as exc:
        print(f"FIBSEM simulator failure: {exc}", file=sys.stderr)
        return 70
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
