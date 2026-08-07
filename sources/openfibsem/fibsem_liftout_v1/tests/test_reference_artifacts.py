from __future__ import annotations

from pathlib import Path

from sources.openfibsem.fibsem_liftout_v1.reference_bundles import (
    STEP_IDS,
    load_packaged_reference_bundles,
)
from sources.openfibsem.fibsem_liftout_v1.scenario import (
    load_fixed_scenarios,
    seeded_scenarios,
)


ROOT = Path(__file__).resolve().parents[4]
NOMINAL = (
    ROOT.parent
    / "instance"
    / "sources"
    / "openfibsem"
    / "fibsem_liftout_v1"
    / "scenarios"
    / "nominal.json"
)
EXPECTED = {
    "nominal",
    "small",
    "large",
    "needle_offset",
    "target_pose",
    "seeded_01",
    "seeded_02",
    "seeded_03",
    "seeded_04",
    "seeded_05",
}


def suite_specs():
    return tuple(load_fixed_scenarios(NOMINAL).values()) + seeded_scenarios(
        5,
        base_seed=47000,
        nominal_path=NOMINAL,
    )


def test_packaged_reference_artifacts_cover_exact_ten_world_suite() -> None:
    bundles = load_packaged_reference_bundles(suite_specs())

    assert set(bundles) == EXPECTED
    assert all(set(bundle.steps) == set(STEP_IDS) for bundle in bundles.values())
    assert all(bundle.identity.file_sha256 for bundle in bundles.values())


def test_packaged_reference_artifacts_load_deterministically() -> None:
    first = load_packaged_reference_bundles(suite_specs())
    second = load_packaged_reference_bundles(suite_specs())

    assert {
        name: bundle.identity.bundle_sha256 for name, bundle in first.items()
    } == {
        name: bundle.identity.bundle_sha256 for name, bundle in second.items()
    }
