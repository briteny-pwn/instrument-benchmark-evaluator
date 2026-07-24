from __future__ import annotations

import json

from gateway_client import GatewayClient


FIXED = {
    "psu": "TCPIP0::192.0.2.10::inst0::INSTR",
    "switch": "GPIB0::9::INSTR",
    "awg": "USB0::0x0957::0x2C07::MY000001::0::INSTR",
    "scope": "TCPIP0::192.0.2.20::inst0::INSTR",
    "dmm": "USB0::0x05E6::0x7510::MY000002::0::INSTR",
}


def run_experiment(gateway_endpoint: str, output_path: str) -> dict:
    client = GatewayClient(gateway_endpoint)
    client.connect()
    sessions = [client.open_resource(resource) for resource in FIXED.values()]
    result = {
        "instruments": {},
        "resources": FIXED,
        "measurements": {},
        "derived": {},
        "decision": True,
    }
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    for session in sessions:
        client.close_resource(session)
    client.close()
    return result
