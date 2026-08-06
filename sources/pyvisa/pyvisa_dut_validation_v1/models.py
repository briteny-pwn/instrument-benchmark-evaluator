from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class SemanticAction:
    kind: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorldSpec:
    world_id: str
    seed: int
    gain: float
    offset_v: float
    dmm_noise_v: float
    scope_noise_v: float
    settle_ms: int
    supply_voltage_v: float
    required_routes: frozenset[str]
    gain_min: float
    gain_max: float
    cross_error_max_v: float
    dmm_format: str = "decimal"
    binary_length_digits: int = 2
    initial_psu_output: bool = False
    initial_awg_output: bool = False
    initial_closed_routes: frozenset[str] = field(default_factory=frozenset)
    resource_map: tuple[tuple[str, str], ...] = ()
    distractors: tuple[tuple[str, str], ...] = ()
    transient_error_role: str | None = None
    transient_error_command: str | None = None
    transient_error_count: int = 0

    @classmethod
    def nominal(cls, seed: int = 1) -> "WorldSpec":
        return cls(
            world_id="nominal",
            seed=seed,
            gain=2.0,
            offset_v=0.0,
            dmm_noise_v=0.001,
            scope_noise_v=0.002,
            settle_ms=250,
            supply_voltage_v=5.0,
            required_routes=frozenset({"1101", "1102"}),
            gain_min=1.97,
            gain_max=2.03,
            cross_error_max_v=0.02,
            resource_map=(
                ("psu", "TCPIP0::192.0.2.10::inst0::INSTR"),
                ("switch", "GPIB0::9::INSTR"),
                ("awg", "USB0::0x0957::0x2C07::MY000001::0::INSTR"),
                ("scope", "TCPIP0::192.0.2.20::inst0::INSTR"),
                ("dmm", "USB0::0x05E6::0x7510::MY000002::0::INSTR"),
            ),
        )


@dataclass(frozen=True)
class WorldSnapshot:
    clock_ms: int
    closed_routes: tuple[str, ...]
    psu_voltage_v: float
    psu_output: bool
    awg_waveform_name: str | None
    awg_points: tuple[float, ...]
    awg_amplitude_vpp: float
    awg_output: bool
    stimulus_started_ms: int | None
    safe: bool
