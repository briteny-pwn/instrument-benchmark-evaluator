from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[4]
VENDOR = ROOT / "vendor" / "pyvisa-sim-iab" / "pyvisa_sim"
LOCAL_INSTRUMENT = ROOT.parent / "instrument"
NESTED_INSTRUMENT = ROOT / "instrument"
INSTRUMENT = Path(
    os.environ.get(
        "IAB_INSTRUMENT_ROOT",
        LOCAL_INSTRUMENT if LOCAL_INSTRUMENT.is_dir() else NESTED_INSTRUMENT,
    )
)
UPSTREAM_WHEEL = (
    INSTRUMENT
    / "container"
    / "wheelhouse"
    / "pyvisa_sim-0.7.1-py3-none-any.whl"
)


class VendoredPyVisaSimTests(unittest.TestCase):
    def test_fork_diff_is_limited_to_approved_hook_surface(self) -> None:
        changed: set[str] = set()
        with ZipFile(UPSTREAM_WHEEL) as archive:
            members = set(archive.namelist())
            for path in VENDOR.rglob("*.py"):
                relative = path.relative_to(VENDOR).as_posix()
                member = f"pyvisa_sim/{relative}"
                if member not in members or path.read_bytes() != archive.read(member):
                    changed.add(relative)
        self.assertEqual(
            changed, {"devices.py", "hooks.py", "sessions/session.py"}
        )

    def test_hook_order_and_atomic_rejection_remain_native(self) -> None:
        import pyvisa
        from pyvisa_sim.hooks import (
            CommandRejected,
            install_hook_provider,
        )

        calls: list[str] = []

        class Provider:
            def before_command(self, context):
                calls.append("before")
                if context.command.startswith(b"VOLT "):
                    raise CommandRejected(-221, "Settings conflict")

            def dynamic_response(self, context, response):
                calls.append("dynamic")
                return response

            def after_command(self, context, response):
                calls.append("after")

            def on_error(self, context, error):
                calls.append("error")

        definition = '''spec: "1.1"
devices:
  device:
    eom: {TCPIP INSTR: {q: "\\n", r: "\\n"}}
    error:
      error_queue:
        - {q: "SYST:ERR?", default: '0,"No error"', command_error: '-221,"Settings conflict"'}
    dialogues: [{q: "*IDN?", r: "IAB,NATIVE,1,1"}]
    properties:
      voltage:
        default: 0.0
        getter: {q: "VOLT?", r: "{:.1f}"}
        setter: {q: "VOLT {:.1f}"}
        specs: {type: float, min: 0, max: 6}
resources:
  TCPIP0::127.0.0.1::inst0::INSTR: {device: device}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sim.yaml"
            path.write_text(definition)
            install_hook_provider(Provider())
            manager = pyvisa.ResourceManager(f"{path}@sim")
            resource = manager.open_resource(
                "TCPIP0::127.0.0.1::inst0::INSTR",
                read_termination="\n",
                write_termination="\n",
            )
            try:
                self.assertEqual(resource.query("*IDN?"), "IAB,NATIVE,1,1")
                self.assertEqual(calls, ["before", "dynamic", "after"])
                with self.assertRaises(CommandRejected):
                    resource.write("VOLT 5.0")
                self.assertEqual(resource.query("VOLT?"), "0.0")
                self.assertIn("error", calls)
            finally:
                resource.close()
                manager.close()
                install_hook_provider(None)


if __name__ == "__main__":
    unittest.main()
