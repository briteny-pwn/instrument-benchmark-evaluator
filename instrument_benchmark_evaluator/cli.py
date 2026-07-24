from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import EVALUATOR_ID, PROTOCOL_VERSION
from .contracts import (
    ContractError,
    RunSettings,
    load_evaluator_request,
    load_instance_settings,
)
from .run import run_full_suite


ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="instrument-evaluator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command != "run":
        return 2
    try:
        request = load_evaluator_request(arguments.request.resolve())
        instance = load_instance_settings(request.instance_path)
        manifest = yaml.safe_load(
            (ROOT / "evaluator.yaml").read_text(encoding="utf-8")
        )
        settings = RunSettings(
            instance_path=request.instance_path,
            fixed_worlds=tuple(manifest["fixed_worlds"]),
            repeated_worlds=request.repeated_worlds,
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
        )
        report = run_full_suite(
            benchmark=settings,
            instance=instance,
            candidate_path=request.candidate_path,
            world_directory=ROOT
            / "evaluators"
            / "pyvisa_dut_validation_v1"
            / "worlds",
            repeated_base_seed=request.repeated_base_seed,
        ).to_dict()
        report["evaluator"] = {
            "id": EVALUATOR_ID,
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


if __name__ == "__main__":
    raise SystemExit(main())
