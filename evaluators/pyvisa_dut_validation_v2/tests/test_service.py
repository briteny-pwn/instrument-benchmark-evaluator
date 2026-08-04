from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pyvisa.constants import ResourceAttribute
from evaluators.pyvisa_dut_validation_v1.models import WorldSpec
from evaluators.pyvisa_dut_validation_v2.protocol import RpcClient
from evaluators.pyvisa_dut_validation_v2.service import run_service
from evaluators.pyvisa_dut_validation_v2.world_contract import dump_world
from instrument_benchmark_evaluator.container.sim_evidence import verify_evidence


ROOT = Path(__file__).resolve().parents[3]
SIMULATOR = ROOT / "evaluators" / "pyvisa_dut_validation_v2" / "simulator.yaml"


class SimServiceTests(unittest.TestCase):
    def test_sigterm_cancels_infinite_read_and_still_finalizes_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "evaluators.pyvisa_dut_validation_v2.broker._peer_credentials",
            return_value=(10001, 10001, 7),
        ):
            root = Path(directory)
            world = root / "world.json"
            evidence = root / "evidence"
            endpoint = root / "transport" / "visa.sock"
            dump_world(WorldSpec.nominal(), world)
            stop = threading.Event()
            stop.iab_signal = "SIGTERM"  # type: ignore[attr-defined]
            result: list[int] = []
            service = threading.Thread(
                target=lambda: result.append(
                    run_service(
                        world=world,
                        endpoint=endpoint,
                        evidence=evidence,
                        simulator=SIMULATOR,
                        run_id="cancel-read",
                        stop_event=stop,
                    )
                )
            )
            service.start()
            deadline = time.monotonic() + 3
            while not endpoint.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            client = RpcClient(str(endpoint))
            manager, _ = client.call("open_default_resource_manager", {})
            resources, _ = client.call(
                "list_resources", {"session": manager, "query": "?*::INSTR"}
            )
            resource, _ = client.call(
                "open",
                {
                    "session": manager,
                    "resource_name": resources[0],
                    "access_mode": 0,
                    "open_timeout": 0,
                },
            )
            client.call(
                "set_attribute",
                {
                    "session": resource,
                    "attribute": int(ResourceAttribute.timeout_value),
                    "attribute_state": 0xFFFFFFFF,
                },
            )
            read_result: list[object] = []
            reader = threading.Thread(
                target=lambda: read_result.append(
                    client.call("read", {"session": resource, "count": 1})
                )
            )
            reader.start()
            time.sleep(0.05)
            started = time.monotonic()
            stop.set()
            service.join(2)
            reader.join(2)
            client.close()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(service.is_alive())
            self.assertFalse(reader.is_alive())
            self.assertEqual(result, [0])
            verified = verify_evidence(
                evidence, run_id="cancel-read", world_id="nominal"
            )
            self.assertTrue(verified.safe)
            self.assertIn(
                "broker.cancellation_requested",
                {event["kind"] for event in verified.events},
            )

    def test_formal_lifecycle_passes_host_evidence_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            world = root / "world.json"
            evidence = root / "evidence"
            endpoint = root / "transport" / "visa.sock"
            dump_world(WorldSpec.nominal(), world)
            stop = threading.Event()
            stop.iab_signal = "SIGTERM"  # type: ignore[attr-defined]
            result = []
            thread = threading.Thread(
                target=lambda: result.append(
                    run_service(
                        world=world,
                        endpoint=endpoint,
                        evidence=evidence,
                        simulator=SIMULATOR,
                        run_id="run",
                        stop_event=stop,
                    )
                )
            )
            thread.start()
            deadline = time.monotonic() + 3
            while not endpoint.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(endpoint.exists())
            stop.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [0])
            verified = verify_evidence(
                evidence, run_id="run", world_id="nominal"
            )
            self.assertTrue(verified.post_cleanup_snapshot["safe"])
            self.assertEqual(verified.open_sessions, 0)
            self.assertEqual(verified.leaked_sessions, 0)
            self.assertTrue(verified.safe)
            self.assertEqual(
                verified.counts["sessions_opened"],
                verified.counts["sessions_explicitly_closed"]
                + verified.counts["sessions_forced_closed"],
            )
            kinds = [event["kind"] for event in verified.events]
            required = [
                "lifecycle.start",
                "lifecycle.configuration",
                "lifecycle.socket_bound",
                "broker.ready",
                "lifecycle.signal",
                "broker.cancellation_requested",
                "broker.frozen",
                "cleanup.pre_snapshot",
                "state.force_safe",
                "cleanup.post_snapshot",
                "lifecycle.summary",
                "lifecycle.finalized",
                "lifecycle.exit",
            ]
            self.assertEqual(
                [kind for kind in kinds if kind in required], required
            )
            self.assertEqual(verified.events[-1]["fields"], {"code": 0, "safe": True})

    def test_pre_stopped_service_exports_complete_safe_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            world = root / "world.json"
            evidence = root / "evidence"
            endpoint = root / "transport" / "visa.sock"
            dump_world(WorldSpec.nominal(), world)
            stop = threading.Event()
            stop.set()
            code = run_service(
                world=world,
                endpoint=endpoint,
                evidence=evidence,
                simulator=SIMULATOR,
                run_id="run",
                stop_event=stop,
            )
            self.assertEqual(code, 0)
            summary = json.loads((evidence / "summary.json").read_text())
            self.assertEqual(summary["world_id"], "nominal")
            self.assertTrue(summary["post_cleanup_snapshot"]["safe"])
            self.assertIn("pre_cleanup_snapshot", summary)
            self.assertTrue((evidence / "events.jsonl").is_file())
            self.assertFalse((evidence / "fatal.json").exists())

    def test_invalid_hidden_world_writes_sanitized_fatal_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            world = root / "world.json"
            evidence = root / "evidence"
            world.write_text("{}")
            code = run_service(
                world=world,
                endpoint=root / "transport" / "visa.sock",
                evidence=evidence,
                simulator=SIMULATOR,
                run_id="run",
                stop_event=threading.Event(),
            )
            self.assertEqual(code, 70)
            fatal = json.loads((evidence / "fatal.json").read_text())
            self.assertEqual(fatal["failure_kind"], "trusted_sim_failure")
            self.assertNotIn("traceback", fatal)
            self.assertNotIn(str(world), json.dumps(fatal))

    def test_trusted_failure_journals_complete_safe_cleanup_before_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "evaluators.pyvisa_dut_validation_v2.service.RemoteVisaBroker.raise_if_failed",
            side_effect=RuntimeError("hidden failure detail"),
        ):
            root = Path(directory)
            world = root / "world.json"
            evidence = root / "evidence"
            dump_world(WorldSpec.nominal(), world)
            stop = threading.Event()
            stop.iab_signal = "SIGTERM"  # type: ignore[attr-defined]
            stop.set()
            code = run_service(
                world=world,
                endpoint=root / "transport" / "visa.sock",
                evidence=evidence,
                simulator=SIMULATOR,
                run_id="fatal-cleanup",
                stop_event=stop,
            )
            self.assertEqual(code, 70)
            events = [
                json.loads(line)
                for line in (evidence / "events.jsonl").read_text().splitlines()
            ]
            kinds = [event["kind"] for event in events]
            self.assertEqual(events[-1]["kind"], "trusted.fatal")
            self.assertLess(
                kinds.index("cleanup.pre_snapshot"),
                kinds.index("state.force_safe"),
            )
            self.assertLess(
                kinds.index("state.force_safe"),
                kinds.index("cleanup.post_snapshot"),
            )
            finalized = next(
                event for event in events
                if event["kind"] == "lifecycle.finalized"
            )
            self.assertTrue(finalized["fields"]["fatal"])
            self.assertTrue(finalized["fields"]["safe"])
            self.assertNotIn(
                "hidden failure detail", (evidence / "fatal.json").read_text()
            )
            self.assertFalse((evidence / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()
