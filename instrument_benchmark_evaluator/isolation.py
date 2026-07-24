from __future__ import annotations

import ast
import shutil
from pathlib import Path

from .contracts import InstanceSettings


class IsolationError(RuntimeError):
    """Candidate material violates the visible or dependency boundary."""


def prepare_workspace(
    instance_root: Path,
    candidate_path: Path,
    config: InstanceSettings,
    workspace: Path,
) -> Path:
    instance_root = instance_root.resolve()
    candidate_path = candidate_path.resolve()
    if workspace.exists() and any(workspace.iterdir()):
        raise IsolationError(f"workspace is not empty: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    for relative in config.visible_files:
        source = _visible_source(instance_root, relative)
        target = workspace / relative
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    if not candidate_path.is_file():
        raise IsolationError(f"candidate does not exist: {candidate_path}")
    shutil.copy2(candidate_path, workspace / config.submission_filename)
    starter_client = workspace / "starter" / "gateway_client.py"
    if starter_client.is_file():
        shutil.copy2(starter_client, workspace / "gateway_client.py")
    scan_forbidden_imports(workspace, config)
    return workspace


def _visible_source(instance_root: Path, relative: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise IsolationError(f"visible path must stay inside instance: {relative}")
    source = (instance_root / raw).resolve()
    if not source.is_relative_to(instance_root):
        raise IsolationError(f"visible path escapes instance: {relative}")
    if not source.exists():
        raise IsolationError(f"visible path does not exist: {relative}")
    return source


def scan_forbidden_imports(workspace: Path, config: InstanceSettings) -> None:
    forbidden = set(config.forbidden_import_roots)
    violations: list[str] = []
    for path in sorted(workspace.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise IsolationError(f"cannot parse candidate file {path}: {exc}") from exc
        for node in ast.walk(tree):
            roots: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                roots = tuple(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = (node.module.split(".", 1)[0],)
            for root in roots:
                if root in forbidden:
                    relative = path.relative_to(workspace)
                    violations.append(f"{relative}:{node.lineno}: forbidden import {root}")
    if violations:
        raise IsolationError("; ".join(violations))
