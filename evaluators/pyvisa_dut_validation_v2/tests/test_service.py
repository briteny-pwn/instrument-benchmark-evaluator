from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from evaluators.pyvisa_dut_validation_v1.models import WorldSpec
from evaluators.pyvisa_dut_validation_v2.service import run_service
from evaluators.pyvisa_dut_validation_v2.world_contract import dump_world


ROOT = Path(__file__).resolve().parents[3]
SIMULATOR = ROOT / "evaluators" / "pyvisa_dut_validation_v2" / "simulator.yaml"


class SimServiceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
