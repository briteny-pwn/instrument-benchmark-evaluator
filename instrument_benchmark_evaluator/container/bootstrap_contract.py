from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BootstrapPaths:
    solution: Path
    endpoint: Path
    output: Path
    returned: Path
    workspace: Path
    output_root: Path


def parse_bootstrap_paths(arguments: list[str]) -> BootstrapPaths:
    if len(arguments) != 4:
        raise ValueError("expected SOLUTION ENDPOINT OUTPUT RETURN")
    values = tuple(Path(raw) for raw in arguments)
    if any(not path.is_absolute() for path in values):
        raise ValueError("all bootstrap paths must be absolute")
    solution, endpoint, output, returned = (
        path.resolve(strict=False) for path in values
    )
    if output == returned or output.parent != returned.parent:
        raise ValueError("result artifacts must be distinct files in one directory")
    if solution.name != "solution.py":
        raise ValueError("candidate entry file must be named solution.py")
    return BootstrapPaths(
        solution=solution,
        endpoint=endpoint,
        output=output,
        returned=returned,
        workspace=solution.parent,
        output_root=output.parent,
    )
