from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from evaluators.pyvisa_dut_validation_v1.models import WorldSnapshot
from evaluators.pyvisa_dut_validation_v2.journal import EventJournal
from instrument_benchmark_evaluator.container.errors import ContainerInfrastructureError
from instrument_benchmark_evaluator.container.sim_evidence import verify_evidence


SAFE = WorldSnapshot(0, (), 0.0, False, None, (), 1.0, False, None, True)


def write_valid(root: Path) -> None:
    journal = EventJournal("run", "world")
    journal.append("lifecycle.start")
    journal.append("broker.ready", endpoint_name="visa.sock")
    journal.append("broker.frozen", connections=0, leaked_sessions=0)
    safe_state = {
        "psu": {"output": False},
        "awg": {"output": False},
        "switch": {"closed_routes": []},
    }
    journal.append(
        "state.force_safe",
        state_before=safe_state,
        state_after=safe_state,
        state_changed=False,
    )
    journal.append(
        "lifecycle.finalized",
        pre_cleanup_snapshot=asdict(SAFE),
        post_cleanup_snapshot=asdict(SAFE),
        broker={"connections": 0, "leaked_sessions": 0, "frozen": True},
    )
    journal.export(root / "events.jsonl")
    summary = {
        "schema_version": 1,
        "run_id": "run",
        "world_id": "world",
        "broker": {"connections": 0, "leaked_sessions": 0, "frozen": True},
        "pre_cleanup_snapshot": asdict(SAFE),
        "post_cleanup_snapshot": asdict(SAFE),
        "event_count": len(journal.events),
        "final_hash": journal.final_hash,
    }
    (root / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, separators=(",", ":"))
    )


class SimEvidenceTests(unittest.TestCase):
    def test_verifies_complete_chain_and_safe_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_valid(root)
            evidence = verify_evidence(root, run_id="run", world_id="world")
            self.assertEqual(evidence.event_count, 5)
            self.assertTrue(evidence.post_cleanup_snapshot["safe"])
            self.assertIsNone(evidence.fatal)

    def test_rejects_tampering_gaps_bad_summary_unsafe_and_symlink(self) -> None:
        mutations = ("event_hash", "sequence", "summary_count", "unsafe")
        for mutation in mutations:
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                write_valid(root)
                if mutation in {"event_hash", "sequence"}:
                    events = [
                        json.loads(line)
                        for line in (root / "events.jsonl").read_text().splitlines()
                    ]
                    events[1][mutation] = "0" * 64 if mutation == "event_hash" else 7
                    (root / "events.jsonl").write_text(
                        "\n".join(json.dumps(item) for item in events) + "\n"
                    )
                else:
                    summary = json.loads((root / "summary.json").read_text())
                    if mutation == "summary_count":
                        summary["event_count"] += 1
                    else:
                        summary["post_cleanup_snapshot"]["safe"] = False
                    (root / "summary.json").write_text(json.dumps(summary))
                with self.assertRaises(ContainerInfrastructureError):
                    verify_evidence(root, run_id="run", world_id="world")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_valid(root)
            target = root / "real-summary.json"
            (root / "summary.json").replace(target)
            (root / "summary.json").symlink_to(target)
            with self.assertRaises(ContainerInfrastructureError):
                verify_evidence(root, run_id="run", world_id="world")

    def test_sanitized_fatal_marker_is_returned_without_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fatal.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run",
                        "failure_kind": "trusted_sim_failure",
                        "exception_type": "RuntimeError",
                        "message": "trusted simulator failed",
                    }
                )
            )
            evidence = verify_evidence(root, run_id="run", world_id="world")
            self.assertEqual(evidence.fatal["failure_kind"], "trusted_sim_failure")
            self.assertEqual(evidence.event_count, 0)

    def test_rejects_missing_summary_wrong_lifecycle_and_oversized_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_valid(root)
            (root / "summary.json").unlink()
            with self.assertRaisesRegex(ContainerInfrastructureError, "incomplete"):
                verify_evidence(root, run_id="run", world_id="world")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_valid(root)
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text().splitlines()
            ]
            events[0]["kind"] = "rpc.request"
            previous = "0" * 64
            for event in events:
                event["previous_hash"] = previous
                unsigned = {
                    key: value
                    for key, value in event.items()
                    if key != "event_hash"
                }
                event["event_hash"] = hashlib.sha256(
                    json.dumps(
                        unsigned, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                previous = event["event_hash"]
            (root / "events.jsonl").write_text(
                "\n".join(
                    json.dumps(item, sort_keys=True, separators=(",", ":"))
                    for item in events
                )
                + "\n"
            )
            summary = json.loads((root / "summary.json").read_text())
            summary["final_hash"] = events[-1]["event_hash"]
            (root / "summary.json").write_text(json.dumps(summary))
            with self.assertRaisesRegex(ContainerInfrastructureError, "lifecycle"):
                verify_evidence(root, run_id="run", world_id="world")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_valid(root)
            with (root / "events.jsonl").open("ab") as stream:
                stream.truncate(16 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(ContainerInfrastructureError, "invalid"):
                verify_evidence(root, run_id="run", world_id="world")


if __name__ == "__main__":
    unittest.main()
