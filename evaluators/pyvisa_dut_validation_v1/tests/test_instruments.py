from __future__ import annotations

import unittest
from pathlib import Path

import pyvisa

from evaluators.pyvisa_dut_validation_v1.instruments import (
    AWG_RESOURCE,
    DMM_RESOURCE,
    PSU_RESOURCE,
    SCOPE_RESOURCE,
    SWITCH_RESOURCE,
    CommandError,
    InstrumentRack,
)
from evaluators.pyvisa_dut_validation_v1.models import WorldSpec


SIMULATOR = (
    Path(__file__).resolve().parents[1] / "simulator" / "base.yaml"
)
STAIRCASE_TEXT = "0,0.3,0.6,0.9,1.2,0.9,0.6,0.3"


def ready_rack() -> InstrumentRack:
    rack = InstrumentRack(WorldSpec.nominal(seed=5))
    rack.write(SWITCH_RESOURCE, b"ROUT:CLOS (@1101,1102)\n")
    rack.write(PSU_RESOURCE, b"INST:NSEL 1\n")
    rack.write(PSU_RESOURCE, b"VOLT 5.0\n")
    rack.write(PSU_RESOURCE, b"OUTP ON\n")
    rack.write(AWG_RESOURCE, f"DATA:ARB DUT_STAIR,{STAIRCASE_TEXT}\n".encode())
    rack.write(AWG_RESOURCE, b"FUNC:ARB DUT_STAIR\n")
    rack.write(AWG_RESOURCE, b"VOLT 1.2\n")
    rack.write(AWG_RESOURCE, b"OUTP ON\n")
    rack.world.advance_ms(rack.world.spec.settle_ms)
    return rack


class InstrumentRackTests(unittest.TestCase):
    def test_pyvisa_sim_definition_exposes_five_resources(self) -> None:
        manager = pyvisa.ResourceManager(f"{SIMULATOR}@sim")
        try:
            self.assertEqual(len(manager.list_resources()), 5)
        finally:
            manager.close()

    def test_resource_order_can_be_changed_without_changing_roles(self) -> None:
        rack = InstrumentRack(WorldSpec.nominal(), resource_order="reversed")

        self.assertEqual(rack.list_resources()[0], DMM_RESOURCE)
        for resource in rack.list_resources():
            self.assertIn(b",", rack.write(resource, b"*IDN?\n"))

    def test_psu_set_query_and_output_update_world(self) -> None:
        rack = InstrumentRack(WorldSpec.nominal())

        rack.write(PSU_RESOURCE, b"INST:NSEL 1\n")
        rack.write(PSU_RESOURCE, b"VOLT 5.0\n")
        rack.write(PSU_RESOURCE, b"OUTP ON\n")

        self.assertEqual(rack.write(PSU_RESOURCE, b"OUTP?\n"), b"1\n")
        self.assertTrue(rack.world.snapshot().psu_output)

    def test_switch_routes_update_shared_world(self) -> None:
        rack = InstrumentRack(WorldSpec.nominal())

        rack.write(SWITCH_RESOURCE, b"ROUT:CLOS (@1101,1102)\n")
        self.assertEqual(
            rack.write(SWITCH_RESOURCE, b"ROUT:CLOS?\n"), b"(@1101,1102)\n"
        )
        rack.write(SWITCH_RESOURCE, b"ROUT:OPEN:ALL\n")

        self.assertEqual(rack.world.snapshot().closed_routes, ())

    def test_awg_upload_requires_selection_before_output(self) -> None:
        rack = InstrumentRack(WorldSpec.nominal())
        rack.write(AWG_RESOURCE, f"DATA:ARB DUT_STAIR,{STAIRCASE_TEXT}\n".encode())

        with self.assertRaisesRegex(CommandError, "selected"):
            rack.write(AWG_RESOURCE, b"OUTP ON\n")

        rack.write(AWG_RESOURCE, b"FUNC:ARB DUT_STAIR\n")
        rack.write(AWG_RESOURCE, b"VOLT 1.2\n")
        rack.write(AWG_RESOURCE, b"OUTP ON\n")
        self.assertEqual(rack.write(AWG_RESOURCE, b"OUTP?\n"), b"1\n")

    def test_dmm_returns_configured_ascii_samples_from_world(self) -> None:
        rack = ready_rack()
        rack.write(DMM_RESOURCE, b"CONF:VOLT:DC\n")
        rack.write(DMM_RESOURCE, b"VOLT:DC:RANG 10\n")
        rack.write(DMM_RESOURCE, b"SAMP:COUN 8\n")
        rack.write(DMM_RESOURCE, b"INIT\n")

        response = rack.write(DMM_RESOURCE, b"READ?\n")

        values = [float(value) for value in response.decode().strip().split(",")]
        self.assertEqual(len(values), 8)
        self.assertAlmostEqual(max(values), 2.4, delta=0.02)

    def test_scope_returns_ieee_block_and_preamble_from_world(self) -> None:
        rack = ready_rack()
        rack.write(SCOPE_RESOURCE, b"DATA:SOURCE CH1\n")
        rack.write(SCOPE_RESOURCE, b"DATA:ENC RIBINARY\n")
        rack.write(SCOPE_RESOURCE, b"DATA:WIDTH 1\n")

        response = rack.write(SCOPE_RESOURCE, b"CURVE?\n")

        self.assertTrue(response.startswith(b"#"))
        digits = int(chr(response[1]))
        payload_length = int(response[2 : 2 + digits])
        payload = response[2 + digits : 2 + digits + payload_length]
        self.assertEqual(payload_length, len(payload))
        self.assertEqual(
            rack.write(SCOPE_RESOURCE, b"WFMOUTPRE:YMULT?\n"), b"0.02\n"
        )

    def test_unknown_command_is_reported_through_error_queue(self) -> None:
        rack = InstrumentRack(WorldSpec.nominal())

        with self.assertRaises(CommandError):
            rack.write(DMM_RESOURCE, b"NOT:A:COMMAND\n")

        self.assertIn(b"-113", rack.write(DMM_RESOURCE, b"SYST:ERR?\n"))
        self.assertEqual(rack.write(DMM_RESOURCE, b"SYST:ERR?\n"), b'0,"No error"\n')


if __name__ == "__main__":
    unittest.main()
