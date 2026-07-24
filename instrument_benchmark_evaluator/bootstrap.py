from __future__ import annotations

import importlib.util
import base64
import json
import os
import socket
import sys
import threading
import time
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
        if event in {
            "subprocess.Popen",
            "os.system",
            "ctypes.dlopen",
            "os.fork",
            "os.forkpty",
            "os.kill",
            "os.exec",
            "os.posix_spawn",
        }:
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
    if os.environ.get("IAB_CONTAINER_MODE") == "1":
        return _supervise(paths)
    return _candidate_main(paths)


def _candidate_main(paths) -> int:
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


def _supervise(paths) -> int:
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(stdout_read)
        os.close(stderr_read)
        os.dup2(stdout_write, 1)
        os.dup2(stderr_write, 2)
        os.close(stdout_write)
        os.close(stderr_write)
        os._exit(_candidate_main(paths))
    os.close(stdout_write)
    os.close(stderr_write)
    pumps = (
        threading.Thread(
            target=_pump_candidate_log,
            args=(stdout_read, sys.stdout.buffer, b"candidate-stdout-b64:"),
        ),
        threading.Thread(
            target=_pump_candidate_log,
            args=(stderr_read, sys.stderr.buffer, b"candidate-stderr-b64:"),
        ),
    )
    for pump in pumps:
        pump.start()
    _, wait_status = os.waitpid(child, 0)
    for pump in pumps:
        pump.join()
    exit_code = os.waitstatus_to_exitcode(wait_status)
    if exit_code != 0:
        return exit_code
    try:
        returned = json.loads(paths.returned.read_text(encoding="utf-8"))
        written = json.loads(paths.output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid supervised artifacts: {exc}", file=sys.stderr)
        return 3
    if not isinstance(returned, dict) or returned != written:
        print("invalid supervised artifacts: result mismatch", file=sys.stderr)
        return 3
    print("\n__IAB_BOOTSTRAP_COMPLETE_V1__", flush=True)
    time.sleep(60)
    return 0


def _pump_candidate_log(descriptor: int, target, prefix: bytes) -> None:
    try:
        while True:
            chunk = os.read(descriptor, 32768)
            if not chunk:
                break
            target.write(prefix + base64.b64encode(chunk) + b"\n")
            target.flush()
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
