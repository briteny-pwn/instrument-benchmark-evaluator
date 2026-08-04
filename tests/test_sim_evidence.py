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
from instrument_benchmark_evaluator.container.sim_evidence import (
    _valid_rpc_boundaries,
    _valid_snapshot,
    verify_evidence,
)


SAFE = WorldSnapshot(0, (), 0.0, False, None, (), 1.0, False, None, True)
ZERO_COUNTS = {
    "connections_opened": 1,
    "connections_closed": 1,
    "connections_rejected": 0,
    "rpc_requests": 1,
    "rpc_results": 1,
    "rpc_rejections": 0,
    "resource_queries": 0,
    "resource_query_results": 0,
    "resource_query_rejections": 0,
    "sessions_opened": 0,
    "sessions_explicitly_closed": 0,
    "sessions_forced_closed": 0,
    "session_invalid_accesses": 0,
    "scpi_writes": 0,
    "scpi_write_results": 0,
    "scpi_reads": 1,
    "scpi_read_results": 1,
}


def write_valid(root: Path, *, signal: str = "SIGTERM") -> None:
    journal = EventJournal("run", "world")
    journal.append("lifecycle.start")
    journal.append(
        "lifecycle.configuration",
        world_sha256="1" * 64,
        simulator_sha256="2" * 64,
    )
    journal.append("lifecycle.socket_bound", endpoint_name="visa.sock", mode="0666")
    journal.append("broker.ready", endpoint_name="visa.sock")
    journal.append("connection.open", connection_id="c")
    journal.append(
        "rpc.request", connection_id="c", operation="read"
    )
    journal.append("scpi.read", connection_id="c", count=1)
    journal.append(
        "scpi.read_result", connection_id="c", payload={}, status=0
    )
    journal.append(
        "rpc.result", connection_id="c", operation="read", status=0
    )
    journal.append("connection.close", connection_id="c")
    journal.append("lifecycle.signal", signal=signal)
    journal.append(
        "broker.cancellation_requested",
        active_workers=0,
        active_connections=0,
    )
    journal.append("broker.frozen", connections=1, leaked_sessions=0)
    safe_state = {
        "psu": {"output": False},
        "awg": {"output": False},
        "switch": {"closed_routes": []},
    }
    journal.append("cleanup.pre_snapshot", snapshot=asdict(SAFE))
    journal.append(
        "state.force_safe",
        state_before=safe_state,
        state_after=safe_state,
        state_changed=False,
    )
    journal.append("cleanup.post_snapshot", snapshot=asdict(SAFE))
    journal.append(
        "lifecycle.summary",
        broker={"connections": 1, "leaked_sessions": 0, "frozen": True},
        counts=ZERO_COUNTS,
        open_sessions=0,
        leaked_sessions=0,
        safe=True,
        fatal=None,
    )
    journal.append(
        "lifecycle.finalized",
        pre_cleanup_snapshot=asdict(SAFE),
        post_cleanup_snapshot=asdict(SAFE),
        broker={"connections": 1, "leaked_sessions": 0, "frozen": True},
        counts=ZERO_COUNTS,
        open_sessions=0,
        leaked_sessions=0,
        safe=True,
        fatal=None,
    )
    journal.append("lifecycle.exit", code=0, safe=True)
    journal.export(root / "events.jsonl")
    summary = {
        "schema_version": 1,
        "run_id": "run",
        "world_id": "world",
        "broker": {"connections": 1, "leaked_sessions": 0, "frozen": True},
        "pre_cleanup_snapshot": asdict(SAFE),
        "post_cleanup_snapshot": asdict(SAFE),
        "event_count": len(journal.events),
        "final_hash": journal.final_hash,
        "counts": ZERO_COUNTS,
        "open_sessions": 0,
        "leaked_sessions": 0,
        "safe": True,
        "fatal": None,
    }
    (root / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, separators=(",", ":"))
    )


class SimEvidenceTests(unittest.TestCase):
    def test_rpc_success_requires_matching_scpi_attempt_and_result(self) -> None:
        events = (
            {
                "kind": "rpc.request",
                "fields": {"connection_id": "c", "operation": "read"},
            },
            {"kind": "scpi.read", "fields": {"connection_id": "c"}},
            {
                "kind": "scpi.read_result",
                "fields": {"connection_id": "c"},
            },
            {
                "kind": "rpc.result",
                "fields": {"connection_id": "c", "operation": "read"},
            },
        )
        self.assertTrue(_valid_rpc_boundaries(events))
        self.assertFalse(_valid_rpc_boundaries(events[:2] + events[3:]))

    def test_snapshot_safe_flag_must_match_outputs_and_routes(self) -> None:
        value = json.loads(json.dumps(asdict(SAFE)))
        self.assertTrue(_valid_snapshot(value))
        for field, unsafe in (
            ("psu_output", True),
            ("awg_output", True),
            ("closed_routes", ["1101"]),
        ):
            with self.subTest(field=field):
                changed = {**value, field: unsafe}
                self.assertFalse(_valid_snapshot(changed))

    def test_verifies_complete_chain_and_safe_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_valid(root)
            evidence = verify_evidence(root, run_id="run", world_id="world")
            self.assertEqual(evidence.event_count, 19)
            self.assertTrue(evidence.post_cleanup_snapshot["safe"])
            self.assertEqual(evidence.counts["rpc_requests"], 1)
            self.assertEqual(evidence.open_sessions, 0)
            self.assertTrue(evidence.safe)
            self.assertIsNone(evidence.fatal)

    def test_rejects_tampering_gaps_bad_summary_unsafe_and_symlink(self) -> None:
        mutations = (
            "event_hash",
            "sequence",
            "summary_count",
            "rpc_count",
            "unsafe",
        )
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
                    elif mutation == "rpc_count":
                        summary["counts"]["rpc_requests"] += 1
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

    def test_normal_lifecycle_requires_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_valid(root, signal="EVENT")
            with self.assertRaisesRegex(ContainerInfrastructureError, "signal"):
                verify_evidence(root, run_id="run", world_id="world")

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
            with self.assertRaisesRegex(
                ContainerInfrastructureError, "event counts|lifecycle"
            ):
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
