from __future__ import annotations

import re
import struct
from collections import deque
from pathlib import Path
from typing import Callable

import pyvisa

from .dut_world import DUTWorld, WorldStateError
from .models import SemanticAction, WorldSpec


PSU_RESOURCE = "TCPIP0::192.0.2.10::inst0::INSTR"
SWITCH_RESOURCE = "GPIB0::9::INSTR"
AWG_RESOURCE = "USB0::0x0957::0x2C07::MY000001::0::INSTR"
SCOPE_RESOURCE = "TCPIP0::192.0.2.20::inst0::INSTR"
DMM_RESOURCE = "USB0::0x05E6::0x7510::MY000002::0::INSTR"

RESOURCE_ROLES = {
    PSU_RESOURCE: "psu",
    SWITCH_RESOURCE: "switch",
    AWG_RESOURCE: "awg",
    SCOPE_RESOURCE: "scope",
    DMM_RESOURCE: "dmm",
}

SIMULATOR_DEFINITION = Path(__file__).with_name("simulator") / "base.yaml"


class CommandError(RuntimeError):
    """A SCPI command is invalid for the instrument's current state."""


class InstrumentModel:
    def __init__(self, identity: str, world: DUTWorld):
        self.identity = identity
        self.world = world
        self.errors: deque[tuple[int, str]] = deque()

    def exchange(self, command: bytes) -> bytes | None:
        try:
            text = command.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            self.errors.append((-101, "Invalid character"))
            raise CommandError("command must be ASCII") from exc
        upper = text.upper()
        if upper == "*IDN?":
            return f"{self.identity}\n".encode("ascii")
        if upper == "*CLS":
            self.errors.clear()
            return None
        if upper == "*RST":
            self.reset()
            return None
        if upper == "SYST:ERR?":
            if self.errors:
                code, message = self.errors.popleft()
                return f'{code},"{message}"\n'.encode("ascii")
            return b'0,"No error"\n'
        try:
            return self.handle(text)
        except (CommandError, ValueError, WorldStateError) as exc:
            self.errors.append((-113, str(exc)))
            if isinstance(exc, CommandError):
                raise
            raise CommandError(str(exc)) from exc

    def handle(self, text: str) -> bytes | None:
        raise CommandError(f"undefined header: {text}")

    def reset(self) -> None:
        raise NotImplementedError


class PSUModel(InstrumentModel):
    def __init__(self, world: DUTWorld):
        super().__init__("IAB,Virtual-E36312A,PSU001,1.0", world)
        self.channel = 1
        self.voltage = 0.0
        self.output = False

    def reset(self) -> None:
        if self.world.psu_output:
            self.world.apply(SemanticAction("psu.output", {"enabled": False}))
        self.channel = 1
        self.voltage = 0.0
        self.output = False
        self.world.apply(SemanticAction("psu.configure", {"voltage_v": 0.0}))

    def handle(self, text: str) -> bytes | None:
        upper = text.upper()
        if upper.startswith("INST:NSEL "):
            channel = int(text.split(None, 1)[1])
            if channel != 1:
                raise CommandError("only DUT supply channel 1 is available")
            self.channel = channel
            return None
        if upper.startswith("VOLT "):
            self.voltage = float(text.split(None, 1)[1])
            self.world.apply(
                SemanticAction("psu.configure", {"voltage_v": self.voltage})
            )
            return None
        if upper in {"VOLT?", "MEAS:VOLT?"}:
            return f"{self.voltage:.6f}\n".encode()
        if upper.startswith("OUTP "):
            enabled = _parse_bool(text.split(None, 1)[1])
            self.world.apply(SemanticAction("psu.output", {"enabled": enabled}))
            self.output = enabled
            return None
        if upper == "OUTP?":
            return b"1\n" if self.output else b"0\n"
        return super().handle(text)


class SwitchModel(InstrumentModel):
    def __init__(self, world: DUTWorld):
        super().__init__("IAB,Virtual-34980A,SW001,1.0", world)

    def reset(self) -> None:
        self.world.apply(SemanticAction("switch.open_all", {}))

    def handle(self, text: str) -> bytes | None:
        upper = text.upper()
        if upper.startswith("ROUT:CLOS "):
            self.world.apply(
                SemanticAction("switch.close", {"routes": _parse_routes(text)})
            )
            return None
        if upper.startswith("ROUT:OPEN ") and upper != "ROUT:OPEN:ALL":
            self.world.apply(
                SemanticAction("switch.open", {"routes": _parse_routes(text)})
            )
            return None
        if upper == "ROUT:OPEN:ALL":
            self.world.apply(SemanticAction("switch.open_all", {}))
            return None
        if upper == "ROUT:CLOS?":
            routes = ",".join(self.world.snapshot().closed_routes)
            return f"(@{routes})\n".encode()
        return super().handle(text)


class AWGModel(InstrumentModel):
    def __init__(self, world: DUTWorld):
        super().__init__("IAB,Virtual-33512B,AWG001,1.0", world)
        self.waveforms: dict[str, tuple[float, ...]] = {}
        self.selected: str | None = None
        self.amplitude_vpp = 1.0
        self.offset_v = 0.0
        self.output = False

    def reset(self) -> None:
        if self.world.awg_output:
            self.world.apply(SemanticAction("awg.output", {"enabled": False}))
        self.waveforms.clear()
        self.selected = None
        self.amplitude_vpp = 1.0
        self.offset_v = 0.0
        self.output = False

    def handle(self, text: str) -> bytes | None:
        upper = text.upper()
        if upper.startswith("DATA:ARB "):
            body = text.split(None, 1)[1]
            name, raw_points = body.split(",", 1)
            points = tuple(float(value) for value in raw_points.split(","))
            if len(points) < 2:
                raise CommandError("arbitrary waveform needs at least two points")
            self.waveforms[name.upper()] = points
            return None
        if upper.startswith("FUNC:ARB "):
            name = text.split(None, 1)[1].upper()
            if name not in self.waveforms:
                raise CommandError("waveform has not been uploaded")
            self.selected = name
            return None
        if upper.startswith("VOLT:OFFS "):
            self.offset_v = float(text.split(None, 1)[1])
            return None
        if upper.startswith("VOLT "):
            self.amplitude_vpp = float(text.split(None, 1)[1])
            if self.amplitude_vpp <= 0:
                raise CommandError("amplitude must be positive")
            return None
        if upper.startswith("OUTP "):
            enabled = _parse_bool(text.split(None, 1)[1])
            if enabled:
                if self.selected is None:
                    raise CommandError("waveform must be selected before output")
                self.world.apply(
                    SemanticAction(
                        "awg.waveform",
                        {
                            "name": self.selected,
                            "points": self.waveforms[self.selected],
                            "amplitude_vpp": self.amplitude_vpp,
                        },
                    )
                )
            self.world.apply(SemanticAction("awg.output", {"enabled": enabled}))
            self.output = enabled
            return None
        if upper == "OUTP?":
            return b"1\n" if self.output else b"0\n"
        if upper == "FUNC:ARB?":
            return f"{self.selected or ''}\n".encode()
        if upper in {"STAT:OPER:COND?", "*OPC?"}:
            started = self.world.snapshot().stimulus_started_ms
            ready = (
                self.output
                and started is not None
                and self.world.snapshot().clock_ms - started >= self.world.spec.settle_ms
            )
            return b"1\n" if ready else b"0\n"
        return super().handle(text)


class DMMModel(InstrumentModel):
    def __init__(self, world: DUTWorld):
        super().__init__("IAB,Virtual-DMM7510,DMM001,1.0", world)
        self.configured = False
        self.range_v = 10.0
        self.sample_count = 1
        self.initiated = False

    def reset(self) -> None:
        self.configured = False
        self.range_v = 10.0
        self.sample_count = 1
        self.initiated = False

    def handle(self, text: str) -> bytes | None:
        upper = text.upper()
        if upper in {"CONF:VOLT:DC", "CONF:VOLT:DC AUTO"}:
            self.configured = True
            return None
        if upper.startswith("VOLT:DC:RANG "):
            self.range_v = float(text.split(None, 1)[1])
            return None
        if upper.startswith("SAMP:COUN "):
            self.sample_count = int(text.split(None, 1)[1])
            if self.sample_count < 1:
                raise CommandError("sample count must be positive")
            return None
        if upper == "INIT":
            if not self.configured:
                raise CommandError("DMM function is not configured")
            self.initiated = True
            return None
        if upper in {"READ?", "FETC?"}:
            if not self.initiated:
                raise CommandError("DMM acquisition has not been initiated")
            values = _resize(self.world.dmm_samples(), self.sample_count)
            self.initiated = False
            if self.world.spec.dmm_format == "scientific":
                fields = [f"{value:+.8E}" for value in values]
            else:
                fields = [f"{value:+.8f}" for value in values]
            return (",".join(fields) + "\n").encode()
        return super().handle(text)


class ScopeModel(InstrumentModel):
    ymult = 0.02
    yoff = 0.0
    yzero = 0.0

    def __init__(self, world: DUTWorld):
        super().__init__("IAB,Virtual-MSO44,SCOPE001,1.0", world)
        self.source = "CH1"
        self.encoding = "RIBINARY"
        self.width = 1

    def reset(self) -> None:
        self.source = "CH1"
        self.encoding = "RIBINARY"
        self.width = 1

    def handle(self, text: str) -> bytes | None:
        upper = text.upper()
        if upper.startswith("DATA:SOURCE "):
            source = text.split(None, 1)[1].upper()
            if source != "CH1":
                raise CommandError("only CH1 observes the DUT")
            self.source = source
            return None
        if upper.startswith("DATA:ENC "):
            encoding = text.split(None, 1)[1].upper()
            if encoding != "RIBINARY":
                raise CommandError("RIBINARY encoding is required")
            self.encoding = encoding
            return None
        if upper.startswith("DATA:WIDTH "):
            width = int(text.split(None, 1)[1])
            if width != 1:
                raise CommandError("only one-byte samples are supported")
            self.width = width
            return None
        if upper == "WFMOUTPRE:YMULT?":
            return f"{self.ymult}\n".encode()
        if upper == "WFMOUTPRE:YOFF?":
            return f"{self.yoff}\n".encode()
        if upper == "WFMOUTPRE:YZERO?":
            return f"{self.yzero}\n".encode()
        if upper == "WFMOUTPRE:NR_PT?":
            return f"{len(self.world.scope_samples())}\n".encode()
        if upper == "CURVE?":
            values = self.world.scope_samples()
            codes = [
                max(-128, min(127, round((value - self.yzero) / self.ymult + self.yoff)))
                for value in values
            ]
            payload = b"".join(struct.pack("b", code) for code in codes)
            digits = max(
                self.world.spec.binary_length_digits, len(str(len(payload)))
            )
            length = str(len(payload)).zfill(digits).encode()
            return b"#" + str(digits).encode() + length + payload
        return super().handle(text)


class DistractorModel(InstrumentModel):
    def reset(self) -> None:
        return None


class InstrumentRack:
    def __init__(
        self,
        spec: WorldSpec,
        *,
        resource_order: str = "normal",
        simulator_definition: Path = SIMULATOR_DEFINITION,
    ):
        self.spec = spec
        self.world = DUTWorld(spec)
        self._simulator_definition = simulator_definition
        self._resource_manager = pyvisa.ResourceManager(
            f"{simulator_definition}@sim"
        )
        listed = tuple(self._resource_manager.list_resources())
        if set(listed) != set(RESOURCE_ROLES):
            self._resource_manager.close()
            raise RuntimeError("pyvisa-sim resources do not match semantic rack")
        role_resources = dict(spec.resource_map) or {
            role: resource for resource, role in RESOURCE_ROLES.items()
        }
        ordered_targets = tuple(resource for _, resource in spec.resource_map)
        if not ordered_targets:
            ordered_targets = tuple(RESOURCE_ROLES)
        resources = ordered_targets + tuple(
            resource for resource, _ in spec.distractors
        )
        self._resources = (
            tuple(reversed(resources)) if resource_order == "reversed" else resources
        )
        model_by_role: dict[str, InstrumentModel] = {
            "psu": PSUModel(self.world),
            "switch": SwitchModel(self.world),
            "awg": AWGModel(self.world),
            "scope": ScopeModel(self.world),
            "dmm": DMMModel(self.world),
        }
        self._models = {
            resource: model_by_role[role] for role, resource in role_resources.items()
        }
        self._roles = {
            resource: role for role, resource in role_resources.items()
        }
        for resource, identity in spec.distractors:
            self._models[resource] = DistractorModel(identity, self.world)
            self._roles[resource] = "distractor"
        self._transient_errors_remaining = spec.transient_error_count

    def list_resources(self) -> tuple[str, ...]:
        return self._resources

    def role_for(self, resource: str) -> str:
        try:
            return self._roles[resource]
        except KeyError as exc:
            raise KeyError(f"unknown resource {resource!r}") from exc

    def write(self, resource: str, payload: bytes) -> bytes | None:
        try:
            model = self._models[resource]
        except KeyError as exc:
            raise KeyError(f"unknown resource {resource!r}") from exc
        if (
            self._transient_errors_remaining > 0
            and self.spec.transient_error_role == self.role_for(resource)
            and self.spec.transient_error_command is not None
            and payload.decode("ascii", errors="ignore").strip().upper()
            == self.spec.transient_error_command.upper()
        ):
            self._transient_errors_remaining -= 1
            raise CommandError("transient communication error")
        return model.exchange(payload)

    def close(self) -> None:
        self._resource_manager.close()

    def force_safe(self) -> None:
        """Place host-owned simulated hardware in a safe state after a run."""
        snapshot = self.world.snapshot()
        if snapshot.awg_output:
            self.world.apply(SemanticAction("awg.output", {"enabled": False}))
        if snapshot.psu_output:
            self.world.apply(SemanticAction("psu.output", {"enabled": False}))
        if self.world.snapshot().closed_routes:
            self.world.apply(SemanticAction("switch.open_all", {}))


def _parse_bool(value: str) -> bool:
    normalized = value.strip().upper()
    if normalized in {"1", "ON"}:
        return True
    if normalized in {"0", "OFF"}:
        return False
    raise CommandError(f"invalid boolean {value!r}")


def _parse_routes(text: str) -> tuple[str, ...]:
    match = re.search(r"\(@([0-9,\s]+)\)", text)
    if match is None:
        raise CommandError("route list must use (@channel,...) syntax")
    routes = tuple(value.strip() for value in match.group(1).split(","))
    if any(not value.isdigit() for value in routes):
        raise CommandError("route values must be numeric")
    return routes


def _resize(values: tuple[float, ...], count: int) -> tuple[float, ...]:
    if not values:
        return ()
    return tuple(values[index % len(values)] for index in range(count))
