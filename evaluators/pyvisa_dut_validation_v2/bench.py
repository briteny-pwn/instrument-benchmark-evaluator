from __future__ import annotations

import base64
import math
import re
import struct
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from pyvisa_sim.component import NoResponse
from pyvisa_sim.highlevel import SimVisaLibrary
from pyvisa_sim.hooks import (
    CommandContext,
    CommandRejected,
    HookProvider,
    install_hook_provider,
)

from evaluators.pyvisa_dut_validation_v1.models import WorldSnapshot, WorldSpec

from .dut import DUTSpec, DynamicDUT
from .journal import EventJournal


MAX_WAVEFORM_POINTS = 64
MAX_WAVEFORMS = 8
MAX_WAVEFORM_NAME_BYTES = 64


class BenchContext(HookProvider):
    def __init__(
        self, definition: Path, spec: WorldSpec, journal: EventJournal
    ) -> None:
        self.spec = spec
        self.journal = journal
        self.dut = DynamicDUT(
            DUTSpec(
                gain=spec.gain,
                offset_v=spec.offset_v,
                settle_ms=spec.settle_ms,
                dmm_noise_v=spec.dmm_noise_v,
                scope_noise_v=spec.scope_noise_v,
                seed=spec.seed,
            )
        )
        self._roles = {resource: role for role, resource in spec.resource_map}
        self._resource_by_role = dict(spec.resource_map)
        self._routes: set[str] = set(spec.initial_closed_routes)
        self._waveforms: dict[str, tuple[float, ...]] = {}
        self._started_ns = time.monotonic_ns()
        self._stimulus_started_ns: int | None = None
        self._dmm_configured = False
        self._dmm_initiated = False
        self._snapshots: dict[int, dict[str, Any]] = {}
        self._session = threading.local()
        self._transient_errors_remaining = spec.transient_error_count
        self._operation_cancelled = threading.Event()
        self._temporary = tempfile.TemporaryDirectory(prefix="iab-sim-")
        rendered = Path(self._temporary.name) / "simulator.yaml"
        try:
            _render_definition(definition, spec, rendered)
            install_hook_provider(self)
            self.visalib = SimVisaLibrary(str(rendered))
            self._apply_initial_state()
        except BaseException:
            install_hook_provider(None)
            self._temporary.cleanup()
            raise

    @classmethod
    def from_world(
        cls, definition: Path, spec: WorldSpec, journal: EventJournal
    ) -> "BenchContext":
        return cls(definition, spec, journal)

    def resource_name(self, role: str) -> str:
        return self._resource_by_role[role]

    @property
    def safe(self) -> bool:
        state = self.snapshot()
        return state.safe

    def snapshot(self) -> WorldSnapshot:
        clock_ms = (time.monotonic_ns() - self._started_ns) // 1_000_000
        selected = str(self._property("awg", "selected")).upper() or None
        points = self._waveforms.get(selected or "", ())
        started = (
            None
            if self._stimulus_started_ns is None
            else (self._stimulus_started_ns - self._started_ns) // 1_000_000
        )
        psu_output = self._output("psu")
        awg_output = self._output("awg")
        routes = tuple(sorted(self._routes))
        return WorldSnapshot(
            clock_ms=clock_ms,
            closed_routes=routes,
            psu_voltage_v=float(self._property("psu", "voltage")),
            psu_output=psu_output,
            awg_waveform_name=selected,
            awg_points=points,
            awg_amplitude_vpp=float(self._property("awg", "amplitude")),
            awg_output=awg_output,
            stimulus_started_ms=started,
            safe=not psu_output and not awg_output and not routes,
        )

    def close(self) -> None:
        if not self.snapshot().safe:
            self.force_safe()
        install_hook_provider(None)
        self._temporary.cleanup()

    def cancel_operations(self) -> None:
        self._operation_cancelled.set()

    def operation_cancelled(self) -> bool:
        return self._operation_cancelled.is_set()

    def force_safe(self) -> None:
        before = self.final_state()
        self._device("psu")._properties["output"].set_value("OFF")
        self._device("awg")._properties["output"].set_value("OFF")
        self._routes.clear()
        self._stimulus_started_ns = None
        self._dmm_initiated = False
        after = self.final_state()
        self.journal.append(
            "state.force_safe",
            state_before=before,
            state_after=after,
            state_changed=before != after,
        )

    @contextmanager
    def session_context(self, digest: str):
        previous = getattr(self._session, "digest", "direct")
        self._session.digest = digest
        try:
            yield
        finally:
            self._session.digest = previous

    def before_command(self, context: CommandContext) -> None:
        before = self.final_state()
        self._snapshots[id(context)] = before
        role = self._role(context)
        command = self._command(context)
        self.journal.append(
            "hook.before",
            session_digest=getattr(self._session, "digest", "direct"),
            resource=context.resource_name,
            role=role,
            command=command,
            state_before=before,
        )
        upper = command.upper()
        if (
            self._transient_errors_remaining > 0
            and self.spec.transient_error_role == role
            and self.spec.transient_error_command is not None
            and upper == self.spec.transient_error_command.upper()
        ):
            self._transient_errors_remaining -= 1
            self._reject("Transient communication error", code=-350)
        if role == "switch" and upper.startswith(("ROUT:CLOS ", "ROUT:OPEN ")):
            self._parse_routes(command)
            if self._output("psu") or self._output("awg"):
                self._reject("Settings conflict")
        elif role == "psu" and upper == "OUTP ON":
            if float(self._property("psu", "voltage")) <= 0:
                self._reject("Voltage must be configured before output")
        elif role == "awg" and upper.startswith("FUNC:ARB "):
            if command.split(None, 1)[1].upper() not in self._waveforms:
                self._reject("Waveform has not been uploaded")
        elif role == "awg" and upper.startswith("DATA:ARB "):
            try:
                name, raw = command.split(None, 1)[1].split(",", 1)
                name = name.strip().upper()
                points = tuple(float(value) for value in raw.split(","))
            except (ValueError, IndexError):
                self._reject("Invalid arbitrary waveform")
            if (
                not name
                or len(name.encode("ascii")) > MAX_WAVEFORM_NAME_BYTES
                or len(points) < 2
                or len(points) > MAX_WAVEFORM_POINTS
                or not all(math.isfinite(value) for value in points)
                or (
                    name not in self._waveforms
                    and len(self._waveforms) >= MAX_WAVEFORMS
                )
            ):
                self._reject("Invalid arbitrary waveform")
        elif role == "awg" and upper == "OUTP ON":
            selected = str(self._property("awg", "selected")).upper()
            if (
                not selected
                or selected not in self._waveforms
                or not self.spec.required_routes.issubset(self._routes)
                or not self._output("psu")
            ):
                self._reject("Bench is not ready for AWG output")
        elif role == "dmm" and upper == "INIT" and not self._dmm_configured:
            self._reject("DMM function is not configured")
        elif role == "dmm" and upper in {"READ?", "FETC?"}:
            if not self._dmm_initiated:
                self._reject("DMM acquisition has not been initiated")
            self._require_ready()
        elif role == "scope" and upper == "CURVE?":
            self._require_ready()

    def dynamic_response(self, context: CommandContext, response):
        role = self._role(context)
        command = self._command(context)
        upper = command.upper()
        self.journal.append(
            "hook.native",
            resource=context.resource_name,
            role=role,
            command=command,
            native_response=_encoded_response(response),
        )
        dynamic = response
        if upper == "OUTP?" and role in {"psu", "awg"}:
            dynamic = b"1" if self._output(role) else b"0"
        elif role == "switch" and upper == "ROUT:CLOS?":
            dynamic = f"(@{','.join(sorted(self._routes))})".encode("ascii")
        elif role == "awg" and upper == "STAT:OPER:COND?":
            dynamic = b"1" if self._is_ready() else b"0"
        elif role == "dmm" and upper in {"READ?", "FETC?"}:
            count = int(self._property("dmm", "sample_count"))
            values = self._resize(self.dut.dmm_samples(self._selected_points()), count)
            template = "{:+.8E}" if self.spec.dmm_format == "scientific" else "{:+.8f}"
            dynamic = ",".join(template.format(value) for value in values).encode("ascii")
        elif role == "scope" and upper == "WFMOUTPRE:NR_PT?":
            dynamic = str(len(self._selected_points())).encode("ascii")
        elif role == "scope" and upper == "CURVE?":
            values = self.dut.scope_samples(self._selected_points())
            payload = b"".join(
                struct.pack("b", max(-128, min(127, round(value / 0.02))))
                for value in values
            )
            digits = max(self.spec.binary_length_digits, len(str(len(payload))))
            dynamic = b"#" + str(digits).encode() + str(len(payload)).zfill(digits).encode() + payload
        self.journal.append(
            "hook.dynamic",
            resource=context.resource_name,
            role=role,
            command=command,
            response=_encoded_response(dynamic),
        )
        return dynamic

    def after_command(self, context: CommandContext, response) -> None:
        role = self._role(context)
        command = self._command(context)
        upper = command.upper()
        if role == "switch" and upper.startswith("ROUT:CLOS "):
            self._routes.update(self._parse_routes(command))
        elif role == "switch" and upper.startswith("ROUT:OPEN "):
            self._routes.difference_update(self._parse_routes(command))
        elif role == "switch" and upper in {"ROUT:OPEN:ALL", "*RST"}:
            self._routes.clear()
        elif role == "awg" and upper.startswith("DATA:ARB "):
            name, raw = command.split(None, 1)[1].split(",", 1)
            self._waveforms[name.strip().upper()] = tuple(
                float(value) for value in raw.split(",")
            )
        elif role == "awg" and upper == "OUTP ON":
            self._stimulus_started_ns = time.monotonic_ns()
        elif role == "awg" and upper in {"OUTP OFF", "*RST"}:
            self._stimulus_started_ns = None
            if upper == "*RST":
                self._waveforms.clear()
        elif role == "dmm" and upper == "CONF:VOLT:DC":
            self._dmm_configured = True
        elif role == "dmm" and upper == "INIT":
            self._dmm_initiated = True
        elif role == "dmm" and upper in {"READ?", "FETC?"}:
            self._dmm_initiated = False
        if upper == "*RST":
            self._reset_role(role)
        elif upper == "*CLS":
            context.device.clear_errors()
        before = self._snapshots.pop(id(context))
        after = self.final_state()
        self.journal.append(
            "hook.after",
            resource=context.resource_name,
            role=role,
            command=command,
            response=_encoded_response(response),
            state_before=before,
            state_after=after,
            state_changed=before != after,
        )

    def on_error(self, context: CommandContext, error: CommandRejected) -> None:
        before = self._snapshots.pop(id(context), self.final_state())
        after = self.final_state()
        self.journal.append(
            "hook.error",
            resource=context.resource_name,
            role=self._role(context),
            command=self._command(context),
            error_code=error.code,
            error_message=error.message,
            state_before=before,
            state_after=after,
            state_changed=before != after,
        )

    def final_state(self) -> dict[str, Any]:
        return {
            "psu": {"voltage": self._property("psu", "voltage"), "output": self._output("psu")},
            "switch": {"closed_routes": sorted(self._routes)},
            "awg": {
                "selected": self._property("awg", "selected"),
                "amplitude": self._property("awg", "amplitude"),
                "output": self._output("awg"),
                "waveforms": {name: list(points) for name, points in self._waveforms.items()},
            },
            "dmm": {"configured": self._dmm_configured, "initiated": self._dmm_initiated, "sample_count": self._property("dmm", "sample_count")},
            "scope": {"source": self._property("scope", "source"), "encoding": self._property("scope", "encoding"), "width": self._property("scope", "width")},
        }

    def _apply_initial_state(self) -> None:
        if self.spec.initial_psu_output:
            self._device("psu")._properties["output"].set_value("ON")
        if self.spec.initial_awg_output:
            self._device("awg")._properties["output"].set_value("ON")

    def _role(self, context: CommandContext) -> str:
        return self._roles.get(context.resource_name, "distractor")

    @staticmethod
    def _command(context: CommandContext) -> str:
        return context.command.decode("ascii").strip()

    def _device(self, role: str):
        return self.visalib.devices[self.resource_name(role)]

    def _property(self, role: str, name: str):
        return self._device(role)._properties[name].get_value()

    def _output(self, role: str) -> bool:
        return str(self._property(role, "output")).upper() == "ON"

    def _reset_role(self, role: str) -> None:
        defaults = {
            "psu": {"channel": "1", "voltage": "0", "output": "OFF"},
            "switch": {"close_request": "(@)", "open_request": "(@)"},
            "awg": {"waveform_data": "", "selected": "", "amplitude": "1", "offset": "0", "output": "OFF"},
            "dmm": {"range": "10", "sample_count": "1"},
            "scope": {"source": "CH1", "encoding": "RIBINARY", "width": "1"},
        }
        if role == "distractor":
            return
        for name, value in defaults[role].items():
            self._device(role)._properties[name].set_value(value)
        if role == "switch":
            self._routes.clear()
        elif role == "awg":
            self._waveforms.clear()
            self._stimulus_started_ns = None
        elif role == "dmm":
            self._dmm_configured = self._dmm_initiated = False

    def _selected_points(self) -> tuple[float, ...]:
        return self._waveforms[str(self._property("awg", "selected")).upper()]

    def _is_ready(self) -> bool:
        return (
            self.spec.required_routes.issubset(self._routes)
            and self._output("psu")
            and self._output("awg")
            and self._stimulus_started_ns is not None
            and (time.monotonic_ns() - self._stimulus_started_ns) // 1_000_000 >= self.spec.settle_ms
        )

    def _require_ready(self) -> None:
        if not self._is_ready():
            self._reject("DUT is not ready")

    @staticmethod
    def _parse_routes(command: str) -> set[str]:
        match = re.search(r"\(@([0-9,]+)\)", command)
        if match is None:
            raise CommandRejected(-222, "Invalid route list")
        return set(match.group(1).split(","))

    @staticmethod
    def _resize(values: tuple[float, ...], count: int) -> tuple[float, ...]:
        return tuple(values[index % len(values)] for index in range(count))

    @staticmethod
    def _reject(message: str, *, code: int = -221) -> None:
        raise CommandRejected(code, message)


def _render_definition(definition: Path, spec: WorldSpec, output: Path) -> None:
    value = yaml.safe_load(definition.read_text(encoding="utf-8"))
    resources: dict[str, dict[str, str]] = {}
    for role, resource in spec.resource_map:
        interface = _interface_name(resource)
        if interface not in {"TCPIP", "GPIB", "USB", "ASRL"}:
            raise ValueError(f"unsupported VISA interface: {resource}")
        value["devices"][role]["eom"] = {f"{interface} INSTR": {"q": "\n", "r": "\n"}}
        resources[resource] = {"device": role}
    for index, (resource, identity) in enumerate(spec.distractors):
        interface = _interface_name(resource)
        name = f"distractor_{index}"
        value["devices"][name] = {
            "eom": {f"{interface} INSTR": {"q": "\n", "r": "\n"}},
            "error": {"error_queue": [{"q": "SYST:ERR?", "default": '0,"No error"', "command_error": '-113,"Undefined header"'}]},
            "dialogues": [{"q": "*IDN?", "r": identity}, {"q": "*RST"}, {"q": "*CLS"}],
        }
        resources[resource] = {"device": name}
    value["resources"] = resources
    output.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _interface_name(resource: str) -> str:
    match = re.match(r"[A-Za-z]+", resource)
    interface = match.group(0).upper() if match else ""
    if interface not in {"TCPIP", "GPIB", "USB", "ASRL"}:
        raise ValueError(f"unsupported VISA interface: {resource}")
    return interface


def _encoded_response(response: Any) -> dict[str, Any] | None:
    if response is NoResponse:
        return None
    payload = bytes(response)
    return {
        "base64": base64.b64encode(payload).decode("ascii"),
        "length": len(payload),
    }
