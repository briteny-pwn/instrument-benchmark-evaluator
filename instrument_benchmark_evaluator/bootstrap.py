from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import traceback
from pathlib import Path


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _install_audit_boundary(workspace: Path, endpoint: Path) -> None:
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
    if len(sys.argv) != 6:
        print(
            "usage: bootstrap.py WORKSPACE SOLUTION ENDPOINT OUTPUT RETURN",
            file=sys.stderr,
        )
        return 2
    workspace = Path(sys.argv[1]).resolve()
    solution_path = (workspace / sys.argv[2]).resolve()
    endpoint = Path(sys.argv[3]).resolve()
    output_path = Path(sys.argv[4]).resolve()
    return_path = Path(sys.argv[5]).resolve()
    if not solution_path.is_relative_to(workspace):
        print("solution path escapes workspace", file=sys.stderr)
        return 2
    _install_audit_boundary(workspace, endpoint)
    sys.path.insert(0, str(workspace))
    try:
        spec = importlib.util.spec_from_file_location("candidate_solution", solution_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load candidate solution")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entrypoint = getattr(module, "run_experiment")
        returned = entrypoint(str(endpoint), str(output_path))
        if not isinstance(returned, dict):
            raise ValueError("run_experiment must return a dictionary")
        if not output_path.is_file():
            raise ValueError("run_experiment did not write result.json")
        written = json.loads(output_path.read_text(encoding="utf-8"))
        if returned != written:
            raise ValueError("returned dictionary must equal written result.json")
        return_path.write_text(
            json.dumps(returned, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return 0
    except ValueError as exc:
        print(f"invalid result: {exc}", file=sys.stderr)
        return 3
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
