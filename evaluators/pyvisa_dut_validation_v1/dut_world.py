from __future__ import annotations

import hashlib
import threading
from collections import Counter
from typing import Any

from .models import SemanticAction, WorldSnapshot, WorldSpec


class WorldStateError(RuntimeError):
    """Raised when an observation is requested in an invalid physical state."""


class DUTWorld:
    def __init__(self, spec: WorldSpec):
        self.spec = spec
        self._lock = threading.RLock()
        self.clock_ms = 0
        self.closed_routes = set(spec.initial_closed_routes)
        self.psu_voltage_v = 0.0
        self.psu_output = spec.initial_psu_output
        self.awg_waveform_name: str | None = None
        self.awg_points: tuple[float, ...] = ()
        self.awg_amplitude_vpp = 0.0
        self.awg_output = spec.initial_awg_output
        self.stimulus_started_ms: int | None = (
            0 if spec.initial_awg_output else None
        )
        self._session_reopens: Counter[str] = Counter()

    def advance_ms(self, milliseconds: int) -> None:
        if not isinstance(milliseconds, int) or milliseconds < 0:
            raise ValueError("milliseconds must be a non-negative integer")
        with self._lock:
            self.clock_ms += milliseconds

    def note_session_reopen(self, role: str) -> None:
        with self._lock:
            self._session_reopens[role] += 1

    def apply(self, action: SemanticAction) -> None:
        with self._lock:
            handlers = {
                "switch.close": self._close_routes,
                "switch.open": self._open_routes,
                "switch.open_all": self._open_all,
                "psu.configure": self._configure_psu,
                "psu.output": self._set_psu_output,
                "awg.waveform": self._set_awg_waveform,
                "awg.output": self._set_awg_output,
            }
            try:
                handler = handlers[action.kind]
            except KeyError as exc:
                raise ValueError(f"unsupported semantic action {action.kind!r}") from exc
            handler(dict(action.arguments))

    def _close_routes(self, arguments: dict[str, Any]) -> None:
        if self.psu_output or self.awg_output:
            raise WorldStateError("source outputs must be disabled before closing routes")
        routes = self._routes(arguments)
        self.closed_routes.update(routes)

    def _open_routes(self, arguments: dict[str, Any]) -> None:
        routes = self._routes(arguments)
        self.closed_routes.difference_update(routes)

    def _open_all(self, arguments: dict[str, Any]) -> None:
        if arguments:
            raise ValueError("switch.open_all takes no arguments")
        self.closed_routes.clear()

    @staticmethod
    def _routes(arguments: dict[str, Any]) -> tuple[str, ...]:
        routes = arguments.get("routes")
        if not isinstance(routes, (tuple, list)) or not routes:
            raise ValueError("routes must be a non-empty sequence")
        normalized = tuple(str(route) for route in routes)
        if any(not route.isdigit() for route in normalized):
            raise ValueError("route identifiers must contain only digits")
        return normalized

    def _configure_psu(self, arguments: dict[str, Any]) -> None:
        voltage = float(arguments["voltage_v"])
        if not 0.0 <= voltage <= 6.0:
            raise ValueError("PSU voltage must be between 0 and 6 V")
        self.psu_voltage_v = voltage

    def _set_psu_output(self, arguments: dict[str, Any]) -> None:
        enabled = arguments.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        if enabled and self.psu_voltage_v <= 0:
            raise WorldStateError("power cannot be enabled before voltage configuration")
        self.psu_output = enabled

    def _set_awg_waveform(self, arguments: dict[str, Any]) -> None:
        name = arguments.get("name")
        points = arguments.get("points")
        amplitude = float(arguments.get("amplitude_vpp", 0.0))
        if not isinstance(name, str) or not name:
            raise ValueError("waveform name is required")
        if not isinstance(points, (tuple, list)) or len(points) < 2:
            raise ValueError("waveform points must contain at least two values")
        normalized = tuple(float(point) for point in points)
        if amplitude <= 0:
            raise ValueError("amplitude_vpp must be positive")
        self.awg_waveform_name = name
        self.awg_points = normalized
        self.awg_amplitude_vpp = amplitude

    def _set_awg_output(self, arguments: dict[str, Any]) -> None:
        enabled = arguments.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        if enabled and not self.awg_points:
            raise WorldStateError("stimulus waveform must be configured first")
        self.awg_output = enabled
        self.stimulus_started_ms = self.clock_ms if enabled else None

    def _require_ready(self) -> None:
        if not self.spec.required_routes.issubset(self.closed_routes):
            raise WorldStateError("required DUT route is open")
        if not self.psu_output:
            raise WorldStateError("DUT power is disabled")
        if not self.awg_output or not self.awg_points:
            raise WorldStateError("stimulus is disabled")
        if (
            self.stimulus_started_ms is None
            or self.clock_ms - self.stimulus_started_ms < self.spec.settle_ms
        ):
            raise WorldStateError("DUT has not settled")

    def _noise(self, channel: str, index: int, amplitude: float) -> float:
        if amplitude == 0:
            return 0.0
        material = f"{self.spec.seed}:{channel}:{index}".encode("ascii")
        raw = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        unit = raw / ((1 << 64) - 1)
        return (2.0 * unit - 1.0) * amplitude

    def _ideal_samples(self) -> tuple[float, ...]:
        return tuple(
            self.spec.offset_v + self.spec.gain * point for point in self.awg_points
        )

    def dmm_samples(self) -> tuple[float, ...]:
        with self._lock:
            self._require_ready()
            return tuple(
                value + self._noise("dmm", index, self.spec.dmm_noise_v)
                for index, value in enumerate(self._ideal_samples())
            )

    def scope_samples(self) -> tuple[float, ...]:
        with self._lock:
            self._require_ready()
            return tuple(
                value + self._noise("scope", index, self.spec.scope_noise_v)
                for index, value in enumerate(self._ideal_samples())
            )

    def snapshot(self) -> WorldSnapshot:
        with self._lock:
            safe = not self.awg_output and not self.psu_output and not self.closed_routes
            return WorldSnapshot(
                clock_ms=self.clock_ms,
                closed_routes=tuple(sorted(self.closed_routes)),
                psu_voltage_v=self.psu_voltage_v,
                psu_output=self.psu_output,
                awg_waveform_name=self.awg_waveform_name,
                awg_points=self.awg_points,
                awg_amplitude_vpp=self.awg_amplitude_vpp,
                awg_output=self.awg_output,
                stimulus_started_ms=self.stimulus_started_ms,
                safe=safe,
            )
