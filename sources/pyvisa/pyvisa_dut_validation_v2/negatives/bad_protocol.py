from __future__ import annotations

import json
import os
import socket


def run_experiment(instrument_endpoint: str, output_path: str) -> dict:
    endpoint = os.environ.get("IAB_VISA_SOCKET", instrument_endpoint)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(endpoint)
        connection.sendall(b"\x00\x10\x00\x01")
    result = {"invalid": "malformed protocol traffic"}
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    return result
