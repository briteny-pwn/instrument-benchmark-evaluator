from __future__ import annotations

import json

import pytest

from sources.openfibsem.fibsem_liftout_v1.reports import ReportError, validate_report
from sources.openfibsem.fibsem_liftout_v1.scoring import aggregate_worlds
from sources.openfibsem.fibsem_liftout_v1.tests.test_scoring import world_report


def complete_report() -> dict[str, object]:
    worlds = [
        world_report(name)
        for name in (
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
        )
    ]
    return aggregate_worlds(worlds).to_dict()


def test_report_schema_version_5_round_trips_breakdowns_and_reference() -> None:
    report = complete_report()

    validated = validate_report(report)
    payload = json.dumps(validated, sort_keys=True, separators=(",", ":"))

    assert validated["schema_version"] == 5
    assert validated["source_id"] == "openfibsem"
    assert validated["strict_pass"] is True
    assert len(validated["worlds"]) == 10
    assert validated["worlds"][0]["candidate_container_evidence"][
        "user"
    ] == "10001:10001"
    assert validated["worlds"][0]["sim_container_evidence"]["user"] == "11001:11001"
    assert validated["worlds"][0]["trusted_evidence"]["journal_head_hash"]
    assert validated["worlds"][0]["step_breakdowns"]["step_1"]["criteria"][
        "sample_global"
    ]["metrics"]["voxel_iou"] == 1.0
    assert validated["worlds"][0]["reference"]["algorithm_version"] == (
        "stl-shape-v1"
    )
    assert json.loads(payload) == validated


def test_report_rejects_unsorted_cap_reasons() -> None:
    report = complete_report()
    report["worlds"][0]["step_breakdowns"]["step_1"]["cap"]["reasons"] = [  # type: ignore[index]
        "z_reason",
        "a_reason",
    ]

    with pytest.raises(ReportError, match="cap reasons"):
        validate_report(report)


def test_report_rejects_missing_checkpoint_evidence_and_wrong_world_suite() -> None:
    report = complete_report()
    report["worlds"][0]["checkpoints"].pop("step_2")  # type: ignore[index]
    with pytest.raises(ReportError, match="checkpoint"):
        validate_report(report)

    report = complete_report()
    report["worlds"].pop()  # type: ignore[union-attr]
    with pytest.raises(ReportError, match="ten worlds"):
        validate_report(report)


def test_report_requires_runtime_terminal_and_geometry_provenance() -> None:
    report = complete_report()
    report["worlds"][0].pop("runtime")  # type: ignore[index]
    with pytest.raises(ReportError, match="world fields"):
        validate_report(report)

    report = complete_report()
    report["worlds"][0]["runtime"]["simulator_uid"] = 10001  # type: ignore[index]
    with pytest.raises(ReportError, match="runtime identities"):
        validate_report(report)

    report = complete_report()
    report["worlds"][0]["checkpoints"]["step_1"]["geometry"][  # type: ignore[index]
        "canonical_geometry_hash"
    ] = "forged"
    with pytest.raises(ReportError, match="geometry hash"):
        validate_report(report)

    report = complete_report()
    report["worlds"][0]["candidate_container_evidence"]["network_mode"] = "bridge"  # type: ignore[index]
    with pytest.raises(ReportError, match="candidate container"):
        validate_report(report)
