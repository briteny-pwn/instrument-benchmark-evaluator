from __future__ import annotations

import json
import statistics
import struct
import time

from gateway_client import GatewayClient, GatewayError


AWG_POINTS = [0.0, 0.3, 0.6, 0.9, 1.2, 0.9, 0.6, 0.3]


def _query(
    client: GatewayClient,
    session: str,
    command: str,
    *,
    attempts: int = 3,
) -> bytes:
    last_error: GatewayError | None = None
    for attempt in range(attempts):
        try:
            client.write(session, command.encode("ascii"))
            return client.read(session)
        except GatewayError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.02 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _role(identity: str) -> str | None:
    fields = [field.strip() for field in identity.split(",")]
    if len(fields) < 2:
        return None
    model = fields[1].upper()
    if "E36312A" in model:
        return "psu"
    if "34980A" in model:
        return "switch"
    if "33512B" in model:
        return "awg"
    if "MSO44" in model:
        return "scope"
    if "DMM7510" in model:
        return "dmm"
    return None


def _decode_scope_block(payload: bytes) -> list[int]:
    if len(payload) < 3 or payload[:1] != b"#":
        raise ValueError("scope response is not a definite-length block")
    digit_byte = payload[1]
    if digit_byte < ord("1") or digit_byte > ord("9"):
        raise ValueError("scope block length-digit count is invalid")
    digits = digit_byte - ord("0")
    if len(payload) < 2 + digits:
        raise ValueError("scope block header is truncated")
    length_field = payload[2 : 2 + digits]
    if not length_field.isdigit():
        raise ValueError("scope block length is not decimal")
    declared = int(length_field)
    start = 2 + digits
    end = start + declared
    if end != len(payload):
        raise ValueError("scope block length does not match payload")
    return [value[0] for value in struct.iter_unpack("b", payload[start:end])]


def run_experiment(gateway_endpoint: str, output_path: str) -> dict:
    client = GatewayClient(gateway_endpoint)
    opened: list[str] = []
    sessions: dict[str, str] = {}
    identities: dict[str, str] = {}
    resources_by_role: dict[str, str] = {}
    result: dict | None = None
    client.connect()
    try:
        for resource in client.list_resources():
            session = client.open_resource(resource)
            opened.append(session)
            client.set_timeout(session, 5000)
            client.set_read_termination(session, "\n")
            client.set_write_termination(session, "\n")
            try:
                identity = _query(client, session, "*IDN?").decode("ascii").strip()
            except (GatewayError, UnicodeDecodeError):
                continue
            role = _role(identity)
            if role is not None:
                if role in sessions:
                    raise RuntimeError(f"duplicate target role {role}")
                sessions[role] = session
                identities[role] = identity
                resources_by_role[role] = resource

        required = {"psu", "switch", "awg", "scope", "dmm"}
        missing = required - set(sessions)
        if missing:
            raise RuntimeError(f"missing target instruments: {sorted(missing)}")

        for role in ("switch", "psu", "awg", "scope", "dmm"):
            client.write(sessions[role], b"*RST")

        client.write(sessions["switch"], b"ROUT:CLOS (@1101,1102)")
        client.write(sessions["psu"], b"INST:NSEL 1")
        client.write(sessions["psu"], b"VOLT 5.0")
        client.write(sessions["psu"], b"OUTP ON")

        points_text = ",".join(f"{value:g}" for value in AWG_POINTS)
        client.write(
            sessions["awg"], f"DATA:ARB DUT_STAIR,{points_text}".encode("ascii")
        )
        client.write(sessions["awg"], b"FUNC:ARB DUT_STAIR")
        client.write(sessions["awg"], b"VOLT 1.2")
        client.write(sessions["awg"], b"VOLT:OFFS 0")
        client.write(sessions["awg"], b"OUTP ON")

        deadline = time.monotonic() + 3.0
        while True:
            if _query(client, sessions["awg"], "STAT:OPER:COND?").strip() == b"1":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("DUT stimulus did not settle before deadline")
            time.sleep(0.02)

        client.write(sessions["dmm"], b"CONF:VOLT:DC")
        client.write(sessions["dmm"], b"VOLT:DC:RANG 10")
        client.write(sessions["dmm"], b"SAMP:COUN 8")
        client.write(sessions["dmm"], b"INIT")
        dmm_payload = _query(client, sessions["dmm"], "READ?")
        dmm_samples = [
            float(field.strip())
            for field in dmm_payload.decode("ascii").strip().split(",")
            if field.strip()
        ]
        if len(dmm_samples) != 8:
            raise ValueError("DMM did not return eight samples")

        client.write(sessions["scope"], b"DATA:SOURCE CH1")
        client.write(sessions["scope"], b"DATA:ENC RIBINARY")
        client.write(sessions["scope"], b"DATA:WIDTH 1")
        ymult = float(_query(client, sessions["scope"], "WFMOUTPRE:YMULT?"))
        yoff = float(_query(client, sessions["scope"], "WFMOUTPRE:YOFF?"))
        yzero = float(_query(client, sessions["scope"], "WFMOUTPRE:YZERO?"))
        raw_codes = _decode_scope_block(
            _query(client, sessions["scope"], "CURVE?")
        )
        scope_voltages = [
            (code - yoff) * ymult + yzero for code in raw_codes
        ]
        if len(scope_voltages) != len(dmm_samples):
            raise ValueError("scope and DMM sample counts differ")

        dmm_average = statistics.fmean(dmm_samples)
        scope_peak_to_peak = max(scope_voltages) - min(scope_voltages)
        input_peak_to_peak = max(AWG_POINTS) - min(AWG_POINTS)
        gain = scope_peak_to_peak / input_peak_to_peak
        max_error = max(
            abs(scope - dmm)
            for scope, dmm in zip(scope_voltages, dmm_samples, strict=True)
        )
        decision = 1.97 <= gain <= 2.03 and max_error <= 0.02
        result = {
            "instruments": identities,
            "resources": resources_by_role,
            "measurements": {
                "awg_points_v": AWG_POINTS,
                "dmm_samples_v": dmm_samples,
                "scope_raw_codes": raw_codes,
                "scope_voltages_v": scope_voltages,
            },
            "derived": {
                "dmm_average_v": dmm_average,
                "scope_peak_to_peak_v": scope_peak_to_peak,
                "gain": gain,
                "max_scope_dmm_error_v": max_error,
            },
            "decision": decision,
        }
    finally:
        if "awg" in sessions:
            try:
                client.write(sessions["awg"], b"OUTP OFF")
            except GatewayError:
                pass
        if "psu" in sessions:
            try:
                client.write(sessions["psu"], b"OUTP OFF")
            except GatewayError:
                pass
        if "switch" in sessions:
            try:
                client.write(sessions["switch"], b"ROUT:OPEN:ALL")
            except GatewayError:
                pass
        for session in reversed(opened):
            try:
                client.close_resource(session)
            except GatewayError:
                pass
        client.close()

    if result is None:
        raise RuntimeError("experiment did not produce a result")
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, sort_keys=True, separators=(",", ":"))
    return result
