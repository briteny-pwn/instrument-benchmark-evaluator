from __future__ import annotations

import json
import statistics
import time

import pyvisa


AWG_POINTS = [0.0, 0.3, 0.6, 0.9, 1.2, 0.9, 0.6, 0.3]


def _query(resource, command: str, *, attempts: int = 3) -> str:
    last_error: pyvisa.errors.VisaIOError | None = None
    for attempt in range(attempts):
        try:
            return resource.query(command)
        except pyvisa.errors.VisaIOError as exc:
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
    for marker, role in (
        ("E36312A", "psu"),
        ("34980A", "switch"),
        ("33512B", "awg"),
        ("MSO44", "scope"),
        ("DMM7510", "dmm"),
    ):
        if marker in model:
            return role
    return None


def run_experiment(instrument_endpoint: str, output_path: str) -> dict:
    del instrument_endpoint
    resource_manager = pyvisa.ResourceManager("@iab")
    opened = []
    instruments = {}
    identities: dict[str, str] = {}
    resources_by_role: dict[str, str] = {}
    result: dict | None = None
    try:
        for resource_name in resource_manager.list_resources():
            resource = resource_manager.open_resource(resource_name)
            opened.append(resource)
            resource.timeout = 5000
            resource.read_termination = "\n"
            resource.write_termination = "\n"
            try:
                identity = _query(resource, "*IDN?").strip()
            except (pyvisa.errors.VisaIOError, UnicodeError):
                continue
            role = _role(identity)
            if role is not None:
                if role in instruments:
                    raise RuntimeError(f"duplicate target role {role}")
                instruments[role] = resource
                identities[role] = identity
                resources_by_role[role] = resource_name

        required = {"psu", "switch", "awg", "scope", "dmm"}
        missing = required - set(instruments)
        if missing:
            raise RuntimeError(f"missing target instruments: {sorted(missing)}")

        for role in ("switch", "psu", "awg", "scope", "dmm"):
            instruments[role].write("*RST")

        instruments["switch"].write("ROUT:CLOS (@1101,1102)")
        instruments["psu"].write("INST:NSEL 1")
        instruments["psu"].write("VOLT 5.0")
        instruments["psu"].write("OUTP ON")

        instruments["awg"].write_ascii_values(
            "DATA:ARB DUT_STAIR,", AWG_POINTS, converter="g", separator=","
        )
        instruments["awg"].write("FUNC:ARB DUT_STAIR")
        instruments["awg"].write("VOLT 1.2")
        instruments["awg"].write("VOLT:OFFS 0")
        instruments["awg"].write("OUTP ON")

        deadline = time.monotonic() + 3.0
        while True:
            if _query(instruments["awg"], "STAT:OPER:COND?").strip() == "1":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("DUT stimulus did not settle before deadline")
            time.sleep(0.02)

        instruments["dmm"].write("CONF:VOLT:DC")
        instruments["dmm"].write("VOLT:DC:RANG 10")
        instruments["dmm"].write("SAMP:COUN 8")
        instruments["dmm"].write("INIT")
        dmm_samples = instruments["dmm"].query_ascii_values(
            "READ?", converter="f", separator=",", container=list
        )
        if len(dmm_samples) != 8:
            raise ValueError("DMM did not return eight samples")

        instruments["scope"].write("DATA:SOURCE CH1")
        instruments["scope"].write("DATA:ENC RIBINARY")
        instruments["scope"].write("DATA:WIDTH 1")
        ymult = float(_query(instruments["scope"], "WFMOUTPRE:YMULT?"))
        yoff = float(_query(instruments["scope"], "WFMOUTPRE:YOFF?"))
        yzero = float(_query(instruments["scope"], "WFMOUTPRE:YZERO?"))
        raw_codes = instruments["scope"].query_binary_values(
            "CURVE?",
            datatype="b",
            is_big_endian=True,
            container=list,
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
            "decision": 1.97 <= gain <= 2.03 and max_error <= 0.02,
        }
    finally:
        for role, command in (
            ("awg", "OUTP OFF"),
            ("psu", "OUTP OFF"),
            ("switch", "ROUT:OPEN:ALL"),
        ):
            if role in instruments:
                try:
                    instruments[role].write(command)
                except pyvisa.errors.VisaIOError:
                    pass
        for resource in reversed(opened):
            try:
                resource.close()
            except pyvisa.errors.VisaIOError:
                pass
        resource_manager.close()

    if result is None:
        raise RuntimeError("experiment did not produce a result")
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, sort_keys=True, separators=(",", ":"))
    return result
