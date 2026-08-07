from __future__ import annotations

import copy
import json
import math
import random
from collections import OrderedDict
from pathlib import Path
from typing import Mapping

from .models import ScenarioSpec


SCENARIO_DIRECTORY = Path(__file__).with_name("scenarios")
FIXED_IDS = ("small", "large", "needle_offset", "target_pose")


def load_fixed_scenarios(public_nominal: Path) -> Mapping[str, ScenarioSpec]:
    worlds: OrderedDict[str, ScenarioSpec] = OrderedDict()
    nominal = ScenarioSpec.from_path(public_nominal)
    if nominal.scenario_id != "nominal":
        raise ValueError("public scenario must be nominal")
    worlds[nominal.scenario_id] = nominal
    for scenario_id in FIXED_IDS:
        path = SCENARIO_DIRECTORY / f"hidden_{scenario_id}.json"
        spec = ScenarioSpec.from_path(path)
        if spec.scenario_id != scenario_id:
            raise ValueError(f"fixed scenario ID mismatch: {path.name}")
        worlds[scenario_id] = spec
    return worlds


def seeded_scenarios(
    count: int, *, base_seed: int, nominal_path: Path
) -> tuple[ScenarioSpec, ...]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("count must be a positive integer")
    if isinstance(base_seed, bool) or not isinstance(base_seed, int) or base_seed < 0:
        raise ValueError("base_seed must be a non-negative integer")
    nominal = ScenarioSpec.from_path(nominal_path)
    worlds: list[ScenarioSpec] = []
    for index in range(count):
        seed = base_seed + index
        generated: ScenarioSpec | None = None
        for attempt in range(100):
            rng = random.Random((seed << 8) + attempt)
            scale = rng.uniform(0.75, 1.25)
            scaled_length = nominal.characteristic_length_um * scale
            needle = tuple(
                _stable_float(rng.uniform(-0.20, 0.20) * scaled_length)
                for _ in range(3)
            )
            translation = _bounded_vector(rng, 0.50 * scaled_length)
            rotation = _bounded_vector(rng, 8.0)
            candidate = ScenarioSpec.from_dict(
                varied_document(
                    nominal.to_dict(),
                    scenario_id=f"seeded_{index + 1:02d}",
                    seed=seed,
                    sample_scale=scale,
                    needle_offset_um=needle,
                    target_translation_um=translation,
                    target_rotation_degrees=rotation,
                )
            )
            if candidate.is_solvable:
                generated = candidate
                break
        if generated is None:
            raise ValueError(f"cannot generate solvable scenario for seed {seed}")
        worlds.append(generated)
    return tuple(worlds)


def varied_document(
    nominal: dict[str, object],
    *,
    scenario_id: str,
    seed: int,
    sample_scale: float = 1.0,
    needle_offset_um: tuple[float, float, float] = (0.0, 0.0, 0.0),
    target_translation_um: tuple[float, float, float] = (0.0, 0.0, 0.0),
    target_rotation_degrees: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> dict[str, object]:
    if not 0.5 <= sample_scale <= 1.5:
        raise ValueError("sample scale is outside generator bounds")
    value = copy.deepcopy(nominal)
    value["scenario_id"] = scenario_id
    value["seed"] = seed
    sample = value["sample"]
    frames = value["frames"]
    needle = value["needle"]
    target = value["target"]
    work_envelopes = value["work_envelopes"]
    assert isinstance(sample, dict)
    assert isinstance(frames, dict)
    assert isinstance(needle, dict)
    assert isinstance(target, dict)
    assert isinstance(work_envelopes, dict)

    sample["dimensions_um"] = _scale_vector(sample["dimensions_um"], sample_scale)
    for name in ("source_bridge", "protected_region"):
        _scale_box(sample[name], sample_scale)
    for name in ("step_1", "step_2"):
        _scale_box(work_envelopes[name], sample_scale)
    _scale_box(needle["joint_region"], sample_scale)
    _scale_box(target["joint_region"], sample_scale)

    needle["initial_offset_um"] = list(needle_offset_um)
    needle_pose = frames["needle"]
    assert isinstance(needle_pose, dict)
    needle_pose["position_um"] = _add_vectors(
        needle_pose["position_um"], needle_offset_um
    )

    target_pose = frames["target_pose"]
    assert isinstance(target_pose, dict)
    target_pose["position_um"] = _add_vectors(
        target_pose["position_um"], target_translation_um
    )
    target_pose["orientation_degrees"] = _add_vectors(
        target_pose["orientation_degrees"], target_rotation_degrees
    )
    return value


def canonical_document(value: dict[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _scale_vector(value: object, scale: float) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("scenario vector is invalid")
    return [_stable_float(float(item) * scale) for item in value]


def _add_vectors(value: object, delta: tuple[float, float, float]) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("scenario vector is invalid")
    return [
        _stable_float(float(item) + change)
        for item, change in zip(value, delta, strict=True)
    ]


def _scale_box(value: object, scale: float) -> None:
    if not isinstance(value, dict):
        raise ValueError("scenario box is invalid")
    value["center_um"] = _scale_vector(value["center_um"], scale)
    value["size_um"] = _scale_vector(value["size_um"], scale)


def _bounded_vector(rng: random.Random, radius: float) -> tuple[float, float, float]:
    raw = tuple(rng.uniform(-1.0, 1.0) for _ in range(3))
    norm = math.sqrt(sum(value * value for value in raw))
    if norm == 0:
        return (0.0, 0.0, 0.0)
    magnitude = rng.uniform(0.0, radius)
    return tuple(
        _stable_float(value * magnitude / norm) for value in raw
    )  # type: ignore[return-value]


def _stable_float(value: float) -> float:
    return round(value, 12)
