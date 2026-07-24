from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import yaml

from ..models import WorldSpec


class WorldConfigError(ValueError):
    """A hidden world definition is invalid."""


def load_world_specs(directory: Path) -> dict[str, WorldSpec]:
    specs: dict[str, WorldSpec] = {}
    for path in sorted(directory.glob("*.yaml")):
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise WorldConfigError(f"{path} must contain a mapping")
        world_id = value.get("world_id")
        seed = value.get("seed")
        overrides = value.get("overrides", {})
        if not isinstance(world_id, str) or not world_id:
            raise WorldConfigError(f"{path} has invalid world_id")
        if not isinstance(seed, int):
            raise WorldConfigError(f"{path} has invalid seed")
        if not isinstance(overrides, dict):
            raise WorldConfigError(f"{path} overrides must be a mapping")
        if world_id in specs:
            raise WorldConfigError(f"duplicate world_id {world_id}")
        spec = _apply_overrides(WorldSpec.nominal(seed), world_id, overrides)
        _validate(spec, path)
        specs[world_id] = spec
    return specs


def _apply_overrides(
    base: WorldSpec, world_id: str, overrides: dict[str, object]
) -> WorldSpec:
    allowed = {
        "gain",
        "offset_v",
        "dmm_noise_v",
        "scope_noise_v",
        "settle_ms",
        "dmm_format",
        "binary_length_digits",
        "initial_psu_output",
        "initial_awg_output",
        "initial_closed_routes",
        "resource_map",
        "distractors",
        "transient_error_role",
        "transient_error_command",
        "transient_error_count",
    }
    unknown = sorted(set(overrides) - allowed)
    if unknown:
        raise WorldConfigError(f"unknown world overrides: {', '.join(unknown)}")
    converted = dict(overrides)
    if "initial_closed_routes" in converted:
        converted["initial_closed_routes"] = frozenset(
            str(item) for item in converted["initial_closed_routes"]
        )
    if "resource_map" in converted:
        mapping = converted["resource_map"]
        if not isinstance(mapping, dict):
            raise WorldConfigError("resource_map must be a mapping")
        converted["resource_map"] = tuple(
            (str(role), str(resource)) for role, resource in mapping.items()
        )
    if "distractors" in converted:
        values = converted["distractors"]
        if not isinstance(values, list):
            raise WorldConfigError("distractors must be a list")
        converted["distractors"] = tuple(
            (str(item["resource"]), str(item["identity"])) for item in values
        )
    return replace(base, world_id=world_id, **converted)


def _validate(spec: WorldSpec, path: Path) -> None:
    roles = [role for role, _ in spec.resource_map]
    resources = [resource for _, resource in spec.resource_map]
    if set(roles) != {"psu", "switch", "awg", "scope", "dmm"} or len(roles) != 5:
        raise WorldConfigError(f"{path} resource_map must define five target roles")
    if len(resources) != len(set(resources)):
        raise WorldConfigError(f"{path} target resources must be unique")
    if not 0 <= len(spec.distractors) <= 3:
        raise WorldConfigError(f"{path} must contain zero through three distractors")
    all_resources = resources + [resource for resource, _ in spec.distractors]
    if len(all_resources) != len(set(all_resources)):
        raise WorldConfigError(f"{path} resources must be unique")
    if spec.dmm_format not in {"decimal", "scientific"}:
        raise WorldConfigError(f"{path} has invalid dmm_format")
    if not 1 <= spec.binary_length_digits <= 3:
        raise WorldConfigError(f"{path} has invalid binary_length_digits")
    if spec.settle_ms < 1:
        raise WorldConfigError(f"{path} settle_ms must be positive")


def repeated_specs(count: int, *, base_seed: int = 10_000) -> tuple[WorldSpec, ...]:
    if count < 1:
        raise ValueError("repeated world count must be positive")
    specs: list[WorldSpec] = []
    roles = ("psu", "switch", "awg", "scope", "dmm")
    for index in range(count):
        seed = base_seed + index
        order = roles[index % len(roles) :] + roles[: index % len(roles)]
        resources_by_role = {
            "psu": f"TCPIP0::198.51.100.{10 + index}::inst0::INSTR",
            "switch": f"GPIB{index % 2}::{8 + index}::INSTR",
            "awg": f"USB0::0x0957::0x2C07::AWG{seed}::0::INSTR",
            "scope": f"TCPIP0::203.0.113.{20 + index}::inst0::INSTR",
            "dmm": f"USB0::0x05E6::0x7510::DMM{seed}::0::INSTR",
        }
        gain = 1.78 if index % 5 == 4 else 2.0 + ((index % 3) - 1) * 0.005
        distractors = (
            (
                f"TCPIP0::192.0.2.{100 + index}::inst0::INSTR",
                f"IAB,Virtual-Logger,D{seed},1.0",
            ),
        ) if index % 2 else ()
        spec = replace(
            WorldSpec.nominal(seed),
            world_id=f"repeated_{index:03d}",
            gain=gain,
            offset_v=((index % 3) - 1) * 0.002,
            dmm_format="scientific" if index % 2 else "decimal",
            binary_length_digits=(index % 3) + 1,
            settle_ms=200 + (index % 4) * 75,
            resource_map=tuple((role, resources_by_role[role]) for role in order),
            distractors=distractors,
            transient_error_role="dmm" if index % 4 == 3 else None,
            transient_error_command="*IDN?" if index % 4 == 3 else None,
            transient_error_count=1 if index % 4 == 3 else 0,
        )
        _validate(spec, Path(f"<generated:{index}>"))
        specs.append(spec)
    return tuple(specs)
