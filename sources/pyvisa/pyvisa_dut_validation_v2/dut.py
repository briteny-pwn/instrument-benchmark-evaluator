"""Deterministic dynamic DUT transfer model."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class DUTSpec:
    gain: float = 2.0
    offset_v: float = 0.0
    settle_ms: int = 250
    dmm_noise_v: float = 0.001
    scope_noise_v: float = 0.002
    seed: int = 1

    def __post_init__(self) -> None:
        numeric = (
            self.gain,
            self.offset_v,
            self.dmm_noise_v,
            self.scope_noise_v,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("DUT numeric parameters must be finite")
        if self.settle_ms < 1:
            raise ValueError("settle_ms must be positive")
        if self.dmm_noise_v < 0 or self.scope_noise_v < 0:
            raise ValueError("noise amplitudes must be non-negative")


class DynamicDUT:
    def __init__(self, spec: DUTSpec):
        self.spec = spec

    def dmm_samples(self, points: Iterable[float]) -> tuple[float, ...]:
        return self._samples(points, "dmm", self.spec.dmm_noise_v)

    def scope_samples(self, points: Iterable[float]) -> tuple[float, ...]:
        return self._samples(points, "scope", self.spec.scope_noise_v)

    def _samples(
        self,
        points: Iterable[float],
        channel: str,
        noise_v: float,
    ) -> tuple[float, ...]:
        normalized = tuple(float(point) for point in points)
        if len(normalized) < 2:
            raise ValueError("waveform must contain at least two points")
        if not all(math.isfinite(point) for point in normalized):
            raise ValueError("waveform points must be finite")
        return tuple(
            self.spec.offset_v
            + self.spec.gain * point
            + self._noise(channel, index, noise_v)
            for index, point in enumerate(normalized)
        )

    def _noise(self, channel: str, index: int, amplitude: float) -> float:
        if amplitude == 0:
            return 0.0
        material = f"{self.spec.seed}:{channel}:{index}".encode("ascii")
        raw = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        unit = raw / ((1 << 64) - 1)
        return (2.0 * unit - 1.0) * amplitude
