from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


def finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def vec3(value: object, name: str) -> tuple[float, float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must contain three finite numbers")
    if len(value) != 3:
        raise ValueError(f"{name} must contain three finite numbers")
    return tuple(finite(item, name) for item in value)  # type: ignore[return-value]


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("scenario numbers must be finite")
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _norm(values: Iterable[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


@dataclass(frozen=True)
class PoseSpec:
    relative_to: str
    position_um: tuple[float, float, float]
    orientation_degrees: tuple[float, float, float]

    @classmethod
    def from_dict(cls, value: object, name: str) -> "PoseSpec":
        if not isinstance(value, Mapping) or set(value) != {
            "relative_to",
            "position_um",
            "orientation_degrees",
        }:
            raise ValueError(f"{name} pose fields are invalid")
        relative_to = value["relative_to"]
        if not isinstance(relative_to, str) or not relative_to:
            raise ValueError(f"{name} relative frame is invalid")
        return cls(
            relative_to,
            vec3(value["position_um"], f"{name}.position_um"),
            vec3(value["orientation_degrees"], f"{name}.orientation_degrees"),
        )


@dataclass(frozen=True)
class AdaptiveTolerances:
    characteristic_length_um: float
    position_um: float
    joint_scale_um: float
    orientation_degrees: float
    safe_retraction_um: float

    @classmethod
    def from_dimensions(
        cls, dimensions_um: Sequence[float]
    ) -> "AdaptiveTolerances":
        dimensions = vec3(dimensions_um, "sample dimensions")
        if min(dimensions) <= 0:
            raise ValueError("sample dimensions must be positive")
        length = math.prod(dimensions) ** (1.0 / 3.0)
        return cls(
            characteristic_length_um=length,
            position_um=min(max(0.08 * length, 0.5), 2.0),
            joint_scale_um=min(max(0.03 * length, 0.2), 1.0),
            orientation_degrees=5.0,
            safe_retraction_um=min(max(0.50 * length, 5.0), 20.0),
        )


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    seed: int
    sample_dimensions_um: tuple[float, float, float]
    needle_offset_um: tuple[float, float, float]
    frames: Mapping[str, PoseSpec]
    data: Mapping[str, object]

    @classmethod
    def from_path(cls, path: Path) -> "ScenarioSpec":
        try:
            value = json.loads(
                Path(path).read_text(encoding="utf-8"),
                parse_constant=lambda item: (_ for _ in ()).throw(
                    ValueError(f"scenario number must be finite: {item}")
                ),
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load scenario: {exc}") from exc
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: object) -> "ScenarioSpec":
        if not isinstance(value, Mapping):
            raise ValueError("scenario must be an object")
        required = {
            "schema_version",
            "scenario_id",
            "seed",
            "units",
            "frames",
            "sample",
            "work_envelopes",
            "needle",
            "target",
            "limits",
            "tolerances",
            "imaging",
            "patterning",
        }
        if set(value) != required:
            raise ValueError("scenario top-level fields are invalid")
        scenario_id, seed = value["scenario_id"], value["seed"]
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ValueError("scenario_id must not be empty")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("scenario seed is invalid")
        frames_value, sample, needle = value["frames"], value["sample"], value["needle"]
        if not isinstance(frames_value, Mapping):
            raise ValueError("scenario frames are invalid")
        if not isinstance(sample, Mapping) or not isinstance(needle, Mapping):
            raise ValueError("scenario sample or needle is invalid")
        dimensions = vec3(sample.get("dimensions_um"), "sample dimensions")
        if min(dimensions) <= 0:
            raise ValueError("sample dimensions must be positive")
        offset = vec3(needle.get("initial_offset_um"), "needle initial offset")
        frames = MappingProxyType(
            {
                str(name): PoseSpec.from_dict(pose, str(name))
                for name, pose in frames_value.items()
            }
        )
        expected_frames = {
            "source",
            "sample",
            "needle",
            "target",
            "coupon",
            "needle_approach",
            "target_pose",
        }
        if set(frames) != expected_frames:
            raise ValueError("scenario named frames are incomplete")
        frozen = _freeze(value)
        assert isinstance(frozen, Mapping)
        result = cls(scenario_id, seed, dimensions, offset, frames, frozen)
        result._validate_semantics()
        return result

    @property
    def tolerances(self) -> AdaptiveTolerances:
        return AdaptiveTolerances.from_dimensions(self.sample_dimensions_um)

    @property
    def characteristic_length_um(self) -> float:
        return self.tolerances.characteristic_length_um

    def to_dict(self) -> dict[str, object]:
        value = _thaw(self.data)
        assert isinstance(value, dict)
        return value

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")

    def sample_scale_from(self, nominal: "ScenarioSpec") -> float:
        ratios = tuple(
            actual / base
            for actual, base in zip(
                self.sample_dimensions_um,
                nominal.sample_dimensions_um,
                strict=True,
            )
        )
        if max(ratios) - min(ratios) > 1e-9:
            raise ValueError("sample variation is not a uniform scale")
        return sum(ratios) / 3.0

    def target_translation_norm_from(self, nominal: "ScenarioSpec") -> float:
        actual = self.frames["target_pose"].position_um
        base = nominal.frames["target_pose"].position_um
        return _norm(a - b for a, b in zip(actual, base, strict=True))

    def target_rotation_norm_from(self, nominal: "ScenarioSpec") -> float:
        actual = self.frames["target_pose"].orientation_degrees
        base = nominal.frames["target_pose"].orientation_degrees
        return _norm(a - b for a, b in zip(actual, base, strict=True))

    def world_position(self, frame: str) -> tuple[float, float, float]:
        seen: set[str] = set()

        def resolve(name: str) -> tuple[float, float, float]:
            if name == "world":
                return (0.0, 0.0, 0.0)
            if name in seen:
                raise ValueError("scenario frame cycle")
            seen.add(name)
            pose = self.frames[name]
            parent = resolve(pose.relative_to)
            return tuple(
                left + right
                for left, right in zip(parent, pose.position_um, strict=True)
            )  # type: ignore[return-value]

        return resolve(frame)

    @property
    def is_solvable(self) -> bool:
        try:
            sample = self.world_position("sample")
            coupon = self.world_position("coupon")
            needle = self.world_position("needle")
            target = self.world_position("target")
            limits = self.data["limits"]
            if not isinstance(limits, Mapping):
                return False
            stage_limits = vec3(limits["stage_delta_um"], "stage limits")
            if _norm(a - b for a, b in zip(sample, coupon, strict=True)) < 2 * self.characteristic_length_um:
                return False
            if any(abs(target[index] - sample[index]) > stage_limits[index] for index in range(3)):
                return False
            sample_radius = 0.5 * _norm(self.sample_dimensions_um)
            if _norm(a - b for a, b in zip(sample, needle, strict=True)) <= sample_radius:
                return False
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def _validate_semantics(self) -> None:
        tolerances = self.data["tolerances"]
        if not isinstance(tolerances, Mapping):
            raise ValueError("scenario tolerances are invalid")
        expected = {
            "characteristic_length": "cuberoot_volume",
            "orientation_degrees": 5.0,
        }
        for name, value in expected.items():
            if tolerances.get(name) != value:
                raise ValueError(f"scenario tolerance {name} is invalid")
        formulas = {
            "position": (0.08, 0.5, 2.0),
            "joint": (0.03, 0.2, 1.0),
            "safe_retraction": (0.50, 5.0, 20.0),
        }
        for name, (relative, minimum, maximum) in formulas.items():
            formula = tolerances.get(name)
            if not isinstance(formula, Mapping) or (
                finite(formula.get("relative"), f"{name}.relative"),
                finite(formula.get("minimum_um"), f"{name}.minimum"),
                finite(formula.get("maximum_um"), f"{name}.maximum"),
            ) != (relative, minimum, maximum):
                raise ValueError(f"scenario tolerance {name} is invalid")
