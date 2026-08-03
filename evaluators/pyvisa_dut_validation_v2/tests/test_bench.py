from __future__ import annotations

import json
import math
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import pyvisa

from evaluators.pyvisa_dut_validation_v1.models import WorldSpec
from evaluators.pyvisa_dut_validation_v1.worlds import load_world_specs
from evaluators.pyvisa_dut_validation_v2.bench import BenchContext
from evaluators.pyvisa_dut_validation_v2.journal import EventJournal
from evaluators.pyvisa_dut_validation_v2.world_contract import (
    WorldContractError,
    dump_world,
    load_world,
)


ROOT = Path(__file__).resolve().parents[3]
SIMULATOR = ROOT / "evaluators" / "pyvisa_dut_validation_v2" / "simulator.yaml"
WORLDS = ROOT / "evaluators" / "pyvisa_dut_validation_v1" / "worlds"
POINTS = (0.0, 0.3, 0.6, 0.9, 1.2, 0.9, 0.6, 0.3)


class WorldContractTests(unittest.TestCase):
    def test_every_fixed_world_round_trips_exactly(self) -> None:
        for spec in load_world_specs(WORLDS).values():
            with self.subTest(world=spec.world_id), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "world.json"
                dump_world(spec, path)
                self.assertEqual(load_world(path), spec)

    def test_rejects_wrong_shape_types_values_symlink_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "world.json"
            dump_world(WorldSpec.nominal(), path)
            baseline = json.loads(path.read_text())
            cases = (
                {key: value for key, value in baseline.items() if key != "seed"},
                {**baseline, "extra": 1},
                {**baseline, "seed": True},
                {**baseline, "gain": math.inf},
                {**baseline, "dmm_format": "hex"},
                {**baseline, "resource_map": [["bad", "GPIB0::1::INSTR"]]},
                {**baseline, "required_routes": ["not-a-route"]},
            )
            for value in cases:
                path.write_text(json.dumps(value))
                with self.assertRaises(WorldContractError):
                    load_world(path)
            path.write_bytes(b" " * 65_537)
            with self.assertRaises(WorldContractError):
                load_world(path)
            target = Path(directory) / "target.json"
            dump_world(WorldSpec.nominal(), target)
            link = Path(directory) / "link.json"
            link.symlink_to(target)
            with self.assertRaises(WorldContractError):
                load_world(link)


class BenchContextTests(unittest.TestCase):
    def make_bench(self, spec: WorldSpec):
        journal = EventJournal("run", spec.world_id)
        bench = BenchContext.from_world(SIMULATOR, spec, journal)
        manager = pyvisa.ResourceManager(bench.visalib)
        return bench, journal, manager

    def open_roles(self, bench, manager):
        return {
            role: manager.open_resource(
                bench.resource_name(role),
                read_termination="\n",
                write_termination="\n",
            )
            for role in ("psu", "switch", "awg", "scope", "dmm")
        }

    def test_every_fixed_world_builds_real_sim_library(self) -> None:
        for spec in load_world_specs(WORLDS).values():
            with self.subTest(world=spec.world_id):
                bench, _, manager = self.make_bench(spec)
                try:
                    expected = tuple(resource for _, resource in spec.resource_map)
                    expected += tuple(resource for resource, _ in spec.distractors)
                    self.assertEqual(manager.list_resources(), expected)
                finally:
                    manager.close()
                    bench.close()

    def test_resource_map_order_distractors_and_dirty_state(self) -> None:
        nominal = WorldSpec.nominal()
        spec = replace(
            nominal,
            world_id="dynamic",
            resource_map=tuple(reversed(nominal.resource_map)),
            distractors=(("GPIB0::3::INSTR", "IAB,DISTRACTOR,1,1"),),
            initial_psu_output=True,
            initial_awg_output=True,
            initial_closed_routes=frozenset({"1199"}),
        )
        bench, _, manager = self.make_bench(spec)
        try:
            self.assertEqual(manager.list_resources(), tuple(r for _, r in spec.resource_map) + ("GPIB0::3::INSTR",))
            distractor = manager.open_resource(
                "GPIB0::3::INSTR", read_termination="\n", write_termination="\n"
            )
            try:
                self.assertEqual(distractor.query("*IDN?"), "IAB,DISTRACTOR,1,1")
            finally:
                distractor.close()
            snapshot = bench.snapshot()
            self.assertTrue(snapshot.psu_output)
            self.assertTrue(snapshot.awg_output)
            self.assertEqual(snapshot.closed_routes, ("1199",))
        finally:
            manager.close()
            bench.close()

    def test_world_formats_dut_and_complete_hook_events(self) -> None:
        spec = replace(
            WorldSpec.nominal(5),
            world_id="formats",
            settle_ms=1,
            dmm_noise_v=0,
            scope_noise_v=0,
            dmm_format="scientific",
            binary_length_digits=3,
        )
        bench, journal, manager = self.make_bench(spec)
        resources = self.open_roles(bench, manager)
        try:
            resources["switch"].write("ROUT:CLOS (@1101,1102)")
            resources["psu"].write("VOLT 5")
            resources["psu"].write("OUTP ON")
            resources["awg"].write("DATA:ARB DUT," + ",".join(map(str, POINTS)))
            resources["awg"].write("FUNC:ARB DUT")
            resources["awg"].write("OUTP ON")
            time.sleep(0.003)
            resources["dmm"].write("CONF:VOLT:DC")
            resources["dmm"].write("SAMP:COUN 8")
            resources["dmm"].write("INIT")
            response = resources["dmm"].query("READ?")
            self.assertIn("E", response)
            resources["scope"].write("CURVE?")
            raw = resources["scope"].read_raw().removesuffix(b"\n")
            self.assertEqual(raw[:2], b"#3")
            self.assertTrue({"hook.before", "hook.native", "hook.dynamic", "hook.after"}.issubset({event.kind for event in journal.events}))
        finally:
            for resource in resources.values():
                resource.close()
            manager.close()
            bench.close()

    def test_transient_error_is_one_shot_atomic_and_force_safe(self) -> None:
        spec = replace(
            WorldSpec.nominal(),
            world_id="transient",
            transient_error_role="dmm",
            transient_error_command="*IDN?",
            transient_error_count=1,
        )
        bench, journal, manager = self.make_bench(spec)
        dmm = manager.open_resource(
            bench.resource_name("dmm"), read_termination="\n", write_termination="\n"
        )
        try:
            from pyvisa_sim.hooks import CommandRejected

            with self.assertRaises(CommandRejected):
                dmm.query("*IDN?")
            self.assertIn("Virtual-DMM", dmm.query("*IDN?"))
            bench.force_safe()
            self.assertTrue(bench.safe)
            self.assertIn("hook.error", {event.kind for event in journal.events})
        finally:
            dmm.close()
            manager.close()
            bench.close()


if __name__ == "__main__":
    unittest.main()
