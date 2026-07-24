from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import traceback
from pathlib import Path

try:
    from instrument_benchmark_evaluator.container.bootstrap_contract import (
        parse_bootstrap_paths,
    )
except ModuleNotFoundError:
    _contract_path = Path(__file__).with_name("container") / "bootstrap_contract.py"
    _contract_spec = importlib.util.spec_from_file_location(
        "_iab_bootstrap_contract", _contract_path
    )
    if _contract_spec is None or _contract_spec.loader is None:
        raise RuntimeError("cannot load bootstrap contract")
    _contract_module = importlib.util.module_from_spec(_contract_spec)
    sys.modules[_contract_spec.name] = _contract_module
    _contract_spec.loader.exec_module(_contract_module)
    parse_bootstrap_paths = _contract_module.parse_bootstrap_paths


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _install_audit_boundary(
    workspace: Path, output_root: Path, endpoint: Path
) -> None:
    runtime_roots = tuple(
        Path(value).resolve()
        for value in {sys.base_prefix, sys.prefix}
        if value
    )

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event == "open" and arguments:
            raw = arguments[0]
            if isinstance(raw, int):
                return
            try:
                path = Path(os.fspath(raw))
            except TypeError:
                return
            if _inside(path, workspace):
                return
            if _inside(path, output_root):
                return
            if any(_inside(path, root) for root in runtime_roots):
                return
            if path.resolve() == Path("/dev/null"):
                return
            raise PermissionError(f"filesystem access denied: {path}")
        if event in {"os.listdir", "os.scandir"} and arguments:
            raw = arguments[0]
            try:
                path = Path(os.fspath(raw))
            except TypeError:
                raise PermissionError("filesystem access denied")
            if _inside(path, workspace):
                return
            if any(_inside(path, root) for root in runtime_roots):
                return
            raise PermissionError(f"filesystem access denied: {path}")
        if event == "socket.connect" and len(arguments) >= 2:
            address = arguments[1]
            if isinstance(address, str) and Path(address).resolve() == endpoint.resolve():
                return
            raise PermissionError(f"network access denied: {address!r}")
        if event in {"subprocess.Popen", "os.system", "ctypes.dlopen"}:
            raise PermissionError(f"process/native access denied: {event}")

    sys.addaudithook(audit)


def main() -> int:
    try:
        paths = parse_bootstrap_paths(sys.argv[1:])
    except ValueError as exc:
        print(
            f"invalid bootstrap invocation: {exc}",
            file=sys.stderr,
        )
        return 2
    _install_audit_boundary(paths.workspace, paths.output_root, paths.endpoint)
    sys.path.insert(0, str(paths.workspace))
    try:
        spec = importlib.util.spec_from_file_location(
            "candidate_solution", paths.solution
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load candidate solution")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entrypoint = getattr(module, "run_experiment", None)
        if not callable(entrypoint):
            print("invalid entrypoint: run_experiment is required", file=sys.stderr)
            return 2
        returned = entrypoint(str(paths.endpoint), str(paths.output))
        if not isinstance(returned, dict):
            raise ValueError("run_experiment must return a dictionary")
        if not paths.output.is_file():
            raise ValueError("run_experiment did not write result.json")
        written = json.loads(paths.output.read_text(encoding="utf-8"))
        if not isinstance(written, dict):
            raise ValueError("result.json must contain an object")
        if returned != written:
            raise ValueError("returned dictionary must equal written result.json")
        paths.returned.write_text(
            json.dumps(returned, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return 0
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"invalid result: {exc}", file=sys.stderr)
        return 3
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
