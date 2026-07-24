from __future__ import annotations

import json

from gateway_client import GatewayClient, GatewayError


def run_experiment(gateway_endpoint: str, output_path: str) -> dict:
    client = GatewayClient(gateway_endpoint)
    client.connect()
    instruments = {}
    resources = {}
    for resource in client.list_resources():
        session = client.open_resource(resource)
        client.set_write_termination(session, "\n")
        try:
            client.write(session, b"*IDN?")
            identity = client.read(session).decode().strip()
        except GatewayError:
            continue
        for role, marker in {
            "psu": "E36312A",
            "switch": "34980A",
            "awg": "33512B",
            "scope": "MSO44",
            "dmm": "DMM7510",
        }.items():
            if marker in identity:
                instruments[role] = identity
                resources[role] = resource
    result = {
        "instruments": instruments,
        "resources": resources,
        "measurements": {
            "awg_points_v": [],
            "dmm_samples_v": [],
            "scope_raw_codes": [],
            "scope_voltages_v": [],
        },
        "derived": {
            "dmm_average_v": 0.0,
            "scope_peak_to_peak_v": 0.0,
            "gain": 0.0,
            "max_scope_dmm_error_v": 0.0,
        },
        "decision": False,
    }
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    return result
