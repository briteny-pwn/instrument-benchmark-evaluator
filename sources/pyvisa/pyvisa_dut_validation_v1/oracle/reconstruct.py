from __future__ import annotations

import base64
import math
import statistics
import struct
from dataclasses import dataclass
from typing import Iterable

from ..gateway.journal import EvidenceEvent
from ..models import WorldSpec


class OracleError(RuntimeError):
    """Hidden evidence is insufficient or malformed."""


@dataclass(frozen=True)
class OracleResult:
    identities: dict[str, str]
    resources: dict[str, str]
    awg_points_v: tuple[float, ...]
    dmm_samples_v: tuple[float, ...]
    scope_raw_codes: tuple[int, ...]
    scope_voltages_v: tuple[float, ...]
    dmm_average_v: float
    scope_peak_to_peak_v: float
    gain: float
    max_scope_dmm_error_v: float
    decision: bool

    def to_candidate_result(self) -> dict[str, object]:
        return {
            "instruments": dict(self.identities),
            "resources": dict(self.resources),
            "measurements": {
                "awg_points_v": list(self.awg_points_v),
                "dmm_samples_v": list(self.dmm_samples_v),
                "scope_raw_codes": list(self.scope_raw_codes),
                "scope_voltages_v": list(self.scope_voltages_v),
            },
            "derived": {
                "dmm_average_v": self.dmm_average_v,
                "scope_peak_to_peak_v": self.scope_peak_to_peak_v,
                "gain": self.gain,
                "max_scope_dmm_error_v": self.max_scope_dmm_error_v,
            },
            "decision": self.decision,
        }


def reconstruct(
    evidence: Iterable[EvidenceEvent], spec: WorldSpec
) -> OracleResult:
    identities: dict[str, str] = {}
    resources: dict[str, str] = {}
    awg_points: tuple[float, ...] | None = None
    dmm_samples: tuple[float, ...] | None = None
    scope_block: bytes | None = None
    ymult: float | None = None
    yoff: float | None = None
    yzero: float | None = None

    for record in evidence:
        if record.operation != "write" or record.request_b64 is None:
            continue
        request = base64.b64decode(record.request_b64)
        response = (
            base64.b64decode(record.response_b64)
            if record.response_b64 is not None
            else None
        )
        command = request.decode("ascii", errors="strict").strip()
        upper = command.upper()
        if (
            upper == "*IDN?"
            and response is not None
            and record.role in {"psu", "switch", "awg", "scope", "dmm"}
        ):
            identities[record.role] = response.decode("ascii").strip()
            if record.resource is not None:
                resources[record.role] = record.resource
        elif record.role == "awg" and upper.startswith("DATA:ARB "):
            body = command.split(None, 1)[1]
            _, raw_points = body.split(",", 1)
            awg_points = tuple(float(value) for value in raw_points.split(","))
        elif record.role == "dmm" and upper in {"READ?", "FETC?"}:
            if response is None:
                raise OracleError("DMM acquisition has no hidden response")
            dmm_samples = _parse_ascii_values(response)
        elif record.role == "scope" and upper == "WFMOUTPRE:YMULT?":
            ymult = _one_float(response, "YMULT")
        elif record.role == "scope" and upper == "WFMOUTPRE:YOFF?":
            yoff = _one_float(response, "YOFF")
        elif record.role == "scope" and upper == "WFMOUTPRE:YZERO?":
            yzero = _one_float(response, "YZERO")
        elif record.role == "scope" and upper == "CURVE?":
            if response is None:
                raise OracleError("scope acquisition has no hidden response")
            scope_block = response

    if set(identities) != {"psu", "switch", "awg", "scope", "dmm"}:
        raise OracleError("not all five target identities are present")
    if awg_points is None or len(awg_points) < 2:
        raise OracleError("uploaded AWG waveform is unavailable")
    if dmm_samples is None or not dmm_samples:
        raise OracleError("DMM samples are unavailable")
    if scope_block is None:
        raise OracleError("scope block is unavailable")
    if ymult is None or yoff is None or yzero is None:
        raise OracleError("scope preamble is incomplete")

    raw_codes = _decode_ieee_signed_bytes(scope_block)
    scope_voltages = tuple((code - yoff) * ymult + yzero for code in raw_codes)
    if len(scope_voltages) != len(dmm_samples):
        raise OracleError("DMM and scope sample counts differ")
    input_peak_to_peak = max(awg_points) - min(awg_points)
    if input_peak_to_peak <= 0:
        raise OracleError("AWG waveform has no amplitude")
    scope_peak_to_peak = max(scope_voltages) - min(scope_voltages)
    gain = scope_peak_to_peak / input_peak_to_peak
    cross_error = max(
        abs(scope - dmm)
        for scope, dmm in zip(scope_voltages, dmm_samples, strict=True)
    )
    decision = (
        spec.gain_min <= gain <= spec.gain_max
        and cross_error <= spec.cross_error_max_v
    )
    return OracleResult(
        identities=identities,
        resources=resources,
        awg_points_v=awg_points,
        dmm_samples_v=dmm_samples,
        scope_raw_codes=raw_codes,
        scope_voltages_v=scope_voltages,
        dmm_average_v=statistics.fmean(dmm_samples),
        scope_peak_to_peak_v=scope_peak_to_peak,
        gain=gain,
        max_scope_dmm_error_v=cross_error,
        decision=decision,
    )


def _parse_ascii_values(payload: bytes) -> tuple[float, ...]:
    try:
        values = tuple(
            float(field.strip())
            for field in payload.decode("ascii").strip().replace(";", ",").split(",")
            if field.strip()
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise OracleError("malformed DMM ASCII response") from exc
    if not values or any(not math.isfinite(value) for value in values):
        raise OracleError("DMM response must contain finite values")
    return values


def _one_float(payload: bytes | None, label: str) -> float:
    if payload is None:
        raise OracleError(f"{label} response is missing")
    try:
        value = float(payload.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise OracleError(f"{label} response is malformed") from exc
    if not math.isfinite(value):
        raise OracleError(f"{label} response is not finite")
    return value


def _decode_ieee_signed_bytes(payload: bytes) -> tuple[int, ...]:
    if len(payload) < 3 or payload[0:1] != b"#":
        raise OracleError("scope response is not an IEEE definite-length block")
    if not 49 <= payload[1] <= 57:
        raise OracleError("scope block length-digit count is invalid")
    digits = payload[1] - 48
    if len(payload) < 2 + digits:
        raise OracleError("scope block header is truncated")
    try:
        declared = int(payload[2 : 2 + digits])
    except ValueError as exc:
        raise OracleError("scope block payload length is malformed") from exc
    start = 2 + digits
    end = start + declared
    if end != len(payload):
        raise OracleError("scope block payload length does not match header")
    return tuple(item[0] for item in struct.iter_unpack("b", payload[start:end]))
