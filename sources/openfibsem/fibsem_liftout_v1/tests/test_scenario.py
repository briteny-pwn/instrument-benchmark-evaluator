from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from sources.openfibsem.fibsem_liftout_v1.models import AdaptiveTolerances, ScenarioSpec
from sources.openfibsem.fibsem_liftout_v1.scenario import (
    load_fixed_scenarios,
    seeded_scenarios,
)


ROOT = Path(__file__).resolve().parents[4]
INSTANCE = ROOT.parent / "instance" / "sources" / "openfibsem" / "fibsem_liftout_v1"
NOMINAL = INSTANCE / "scenarios" / "nominal.json"
SCHEMA = INSTANCE / "scenario.schema.json"


def test_tolerances_scale_and_clamp() -> None:
    nominal = AdaptiveTolerances.from_dimensions((14.0, 8.0, 10.0))
    assert nominal.position_um == pytest.approx(0.830799, rel=1e-5)
    assert nominal.joint_scale_um == pytest.approx(0.31155, rel=1e-4)
    assert nominal.orientation_degrees == 5.0
    assert nominal.safe_retraction_um == pytest.approx(5.192494, rel=1e-5)

    assert AdaptiveTolerances.from_dimensions((1.0, 1.0, 1.0)).position_um == 0.5
    assert AdaptiveTolerances.from_dimensions((1.0, 1.0, 1.0)).joint_scale_um == 0.2
    assert AdaptiveTolerances.from_dimensions((100.0, 100.0, 100.0)).position_um == 2.0
    assert AdaptiveTolerances.from_dimensions((100.0, 100.0, 100.0)).joint_scale_um == 1.0
    assert AdaptiveTolerances.from_dimensions((100.0, 100.0, 100.0)).safe_retraction_um == 20.0


def test_fixed_worlds_are_complete_public_schema_documents() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    worlds = load_fixed_scenarios(NOMINAL)

    assert tuple(worlds) == (
        "nominal",
        "small",
        "large",
        "needle_offset",
        "target_pose",
    )
    for world in worlds.values():
        validator.validate(world.to_dict())
        assert ScenarioSpec.from_dict(world.to_dict()) == world
        assert world.is_solvable

    nominal = worlds["nominal"]
    assert worlds["small"].sample_scale_from(nominal) == pytest.approx(0.75)
    assert worlds["large"].sample_scale_from(nominal) == pytest.approx(1.25)
    assert max(abs(value) for value in worlds["needle_offset"].needle_offset_um) <= (
        0.20 * nominal.characteristic_length_um + 1e-9
    )
    assert worlds["target_pose"].target_translation_norm_from(nominal) <= (
        0.50 * nominal.characteristic_length_um + 1e-9
    )
    assert worlds["target_pose"].target_rotation_norm_from(nominal) <= 8.0


def test_seeded_worlds_are_deterministic_bounded_and_unique() -> None:
    first = seeded_scenarios(5, base_seed=47000, nominal_path=NOMINAL)
    second = seeded_scenarios(5, base_seed=47000, nominal_path=NOMINAL)

    assert [world.canonical_bytes() for world in first] == [
        world.canonical_bytes() for world in second
    ]
    assert [world.scenario_id for world in first] == [
        "seeded_01",
        "seeded_02",
        "seeded_03",
        "seeded_04",
        "seeded_05",
    ]
    assert len({world.canonical_bytes() for world in first}) == 5

    nominal = ScenarioSpec.from_path(NOMINAL)
    for world in first:
        assert 0.75 <= world.sample_scale_from(nominal) <= 1.25
        assert max(abs(value) for value in world.needle_offset_um) <= 0.20 * world.characteristic_length_um
        assert world.target_translation_norm_from(nominal) <= 0.50 * world.characteristic_length_um
        assert world.target_rotation_norm_from(nominal) <= 8.0
        assert world.is_solvable


def test_generator_rejects_invalid_counts_and_non_finite_scenarios() -> None:
    with pytest.raises(ValueError, match="count"):
        seeded_scenarios(0, base_seed=47000, nominal_path=NOMINAL)

    value = json.loads(NOMINAL.read_text(encoding="utf-8"))
    value["frames"]["needle"]["position_um"][0] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        ScenarioSpec.from_dict(value)
