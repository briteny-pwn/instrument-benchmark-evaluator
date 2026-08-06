from __future__ import annotations

import unittest

from sources.pyvisa.pyvisa_dut_validation_v1.dut_world import DUTWorld, WorldStateError
from sources.pyvisa.pyvisa_dut_validation_v1.models import SemanticAction, WorldSpec


STAIRCASE = (0.0, 0.3, 0.6, 0.9, 1.2, 0.9, 0.6, 0.3)


def ready_world(seed: int = 7, *, settle: bool = True) -> DUTWorld:
    world = DUTWorld(WorldSpec.nominal(seed=seed))
    world.apply(SemanticAction("switch.close", {"routes": ("1101", "1102")}))
    world.apply(SemanticAction("psu.configure", {"voltage_v": 5.0}))
    world.apply(SemanticAction("psu.output", {"enabled": True}))
    world.apply(
        SemanticAction(
            "awg.waveform",
            {"name": "DUT_STAIR", "points": STAIRCASE, "amplitude_vpp": 1.2},
        )
    )
    world.apply(SemanticAction("awg.output", {"enabled": True}))
    if settle:
        world.advance_ms(world.spec.settle_ms)
    return world


class DUTWorldTests(unittest.TestCase):
    def test_measurement_requires_route_power_stimulus_and_settling(self) -> None:
        world = DUTWorld(WorldSpec.nominal(seed=7))

        with self.assertRaisesRegex(WorldStateError, "route"):
            world.dmm_samples()

        world.apply(SemanticAction("switch.close", {"routes": ("1101", "1102")}))
        with self.assertRaisesRegex(WorldStateError, "power"):
            world.dmm_samples()

        world.apply(SemanticAction("psu.configure", {"voltage_v": 5.0}))
        world.apply(SemanticAction("psu.output", {"enabled": True}))
        with self.assertRaisesRegex(WorldStateError, "stimulus"):
            world.dmm_samples()

        world.apply(
            SemanticAction(
                "awg.waveform",
                {"name": "DUT_STAIR", "points": STAIRCASE, "amplitude_vpp": 1.2},
            )
        )
        world.apply(SemanticAction("awg.output", {"enabled": True}))
        with self.assertRaisesRegex(WorldStateError, "settled"):
            world.dmm_samples()

        world.advance_ms(world.spec.settle_ms)
        self.assertEqual(len(world.dmm_samples()), len(STAIRCASE))

    def test_measurements_are_causally_derived_from_waveform_and_gain(self) -> None:
        world = ready_world(11)
        dmm = world.dmm_samples()
        scope = world.scope_samples()

        self.assertEqual(len(dmm), len(STAIRCASE))
        self.assertEqual(len(scope), len(STAIRCASE))
        self.assertAlmostEqual(max(scope), 2.4, delta=0.02)
        self.assertAlmostEqual(dmm[2], 1.2, delta=0.02)

    def test_reopen_does_not_reset_world(self) -> None:
        world = ready_world(9)
        before = world.snapshot()

        world.note_session_reopen("psu")

        self.assertEqual(world.snapshot(), before)

    def test_same_seed_reproduces_measurements(self) -> None:
        self.assertEqual(ready_world(42).dmm_samples(), ready_world(42).dmm_samples())
        self.assertEqual(
            ready_world(42).scope_samples(), ready_world(42).scope_samples()
        )

    def test_safe_cleanup_snapshot(self) -> None:
        world = ready_world()
        world.apply(SemanticAction("awg.output", {"enabled": False}))
        world.apply(SemanticAction("psu.output", {"enabled": False}))
        world.apply(SemanticAction("switch.open_all", {}))

        snapshot = world.snapshot()

        self.assertFalse(snapshot.awg_output)
        self.assertFalse(snapshot.psu_output)
        self.assertEqual(snapshot.closed_routes, ())
        self.assertTrue(snapshot.safe)


if __name__ == "__main__":
    unittest.main()
