from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import fields
from pathlib import Path
from typing import Any

from sources.pyvisa.pyvisa_dut_validation_v1.models import WorldSpec


MAX_WORLD_BYTES = 65_536
WORLD_KEYS = frozenset(field.name for field in fields(WorldSpec))
ROLES = frozenset({"psu", "switch", "awg", "scope", "dmm"})


class WorldContractError(ValueError):
    pass


def dump_world(spec: WorldSpec, path: Path) -> None:
    value = {
        "world_id": spec.world_id,
        "seed": spec.seed,
        "gain": spec.gain,
        "offset_v": spec.offset_v,
        "dmm_noise_v": spec.dmm_noise_v,
        "scope_noise_v": spec.scope_noise_v,
        "settle_ms": spec.settle_ms,
        "supply_voltage_v": spec.supply_voltage_v,
        "required_routes": sorted(spec.required_routes),
        "gain_min": spec.gain_min,
        "gain_max": spec.gain_max,
        "cross_error_max_v": spec.cross_error_max_v,
        "dmm_format": spec.dmm_format,
        "binary_length_digits": spec.binary_length_digits,
        "initial_psu_output": spec.initial_psu_output,
        "initial_awg_output": spec.initial_awg_output,
        "initial_closed_routes": sorted(spec.initial_closed_routes),
        "resource_map": [list(item) for item in spec.resource_map],
        "distractors": [list(item) for item in spec.distractors],
        "transient_error_role": spec.transient_error_role,
        "transient_error_command": spec.transient_error_command,
        "transient_error_count": spec.transient_error_count,
    }
    _validate_value(value)
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(payload) > MAX_WORLD_BYTES:
        raise WorldContractError("world definition is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_world(path: Path) -> WorldSpec:
    if path.is_symlink():
        raise WorldContractError("world definition must be a regular file")
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size > MAX_WORLD_BYTES:
            raise WorldContractError("invalid world definition file")
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorldContractError("cannot read world definition") from exc
    _validate_value(value)
    return WorldSpec(
        world_id=value["world_id"],
        seed=value["seed"],
        gain=float(value["gain"]),
        offset_v=float(value["offset_v"]),
        dmm_noise_v=float(value["dmm_noise_v"]),
        scope_noise_v=float(value["scope_noise_v"]),
        settle_ms=value["settle_ms"],
        supply_voltage_v=float(value["supply_voltage_v"]),
        required_routes=frozenset(value["required_routes"]),
        gain_min=float(value["gain_min"]),
        gain_max=float(value["gain_max"]),
        cross_error_max_v=float(value["cross_error_max_v"]),
        dmm_format=value["dmm_format"],
        binary_length_digits=value["binary_length_digits"],
        initial_psu_output=value["initial_psu_output"],
        initial_awg_output=value["initial_awg_output"],
        initial_closed_routes=frozenset(value["initial_closed_routes"]),
        resource_map=tuple(tuple(item) for item in value["resource_map"]),
        distractors=tuple(tuple(item) for item in value["distractors"]),
        transient_error_role=value["transient_error_role"],
        transient_error_command=value["transient_error_command"],
        transient_error_count=value["transient_error_count"],
    )


def _validate_value(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != WORLD_KEYS:
        raise WorldContractError("world fields do not match schema")
    if not isinstance(value["world_id"], str) or not value["world_id"]:
        raise WorldContractError("invalid world_id")
    for name in ("seed", "settle_ms", "binary_length_digits", "transient_error_count"):
        if isinstance(value[name], bool) or not isinstance(value[name], int):
            raise WorldContractError(f"invalid {name}")
    if value["settle_ms"] < 1 or not 1 <= value["binary_length_digits"] <= 3:
        raise WorldContractError("invalid timing or binary format")
    if value["transient_error_count"] < 0:
        raise WorldContractError("invalid transient error count")
    for name in (
        "gain", "offset_v", "dmm_noise_v", "scope_noise_v", "supply_voltage_v",
        "gain_min", "gain_max", "cross_error_max_v",
    ):
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise WorldContractError(f"invalid {name}")
    if value["dmm_noise_v"] < 0 or value["scope_noise_v"] < 0:
        raise WorldContractError("noise must be non-negative")
    if value["dmm_format"] not in {"decimal", "scientific"}:
        raise WorldContractError("invalid dmm_format")
    for name in ("initial_psu_output", "initial_awg_output"):
        if not isinstance(value[name], bool):
            raise WorldContractError(f"invalid {name}")
    for name in ("required_routes", "initial_closed_routes"):
        routes = value[name]
        if not isinstance(routes, list) or not all(
            isinstance(route, str) and route.isdigit() for route in routes
        ) or len(routes) != len(set(routes)):
            raise WorldContractError(f"invalid {name}")
    mapping = value["resource_map"]
    if not _pairs(mapping) or len(mapping) != 5 or {item[0] for item in mapping} != ROLES:
        raise WorldContractError("resource_map must define five roles")
    distractors = value["distractors"]
    if not _pairs(distractors) or len(distractors) > 3:
        raise WorldContractError("invalid distractors")
    resources = [item[1] for item in mapping] + [item[0] for item in distractors]
    if len(resources) != len(set(resources)):
        raise WorldContractError("resource names must be unique")
    role = value["transient_error_role"]
    command = value["transient_error_command"]
    if role is not None and role not in ROLES:
        raise WorldContractError("invalid transient error role")
    if command is not None and (not isinstance(command, str) or not command):
        raise WorldContractError("invalid transient error command")
    if value["transient_error_count"] and (role is None or command is None):
        raise WorldContractError("incomplete transient error")


def _pairs(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, list)
        and len(item) == 2
        and all(isinstance(part, str) and part for part in item)
        for item in value
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorldContractError("duplicate world field")
        result[key] = value
    return result
