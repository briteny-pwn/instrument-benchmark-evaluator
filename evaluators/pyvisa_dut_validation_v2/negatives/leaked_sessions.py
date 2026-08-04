from __future__ import annotations

import json
import os

import pyvisa


_LEAKS = []


def run_experiment(instrument_endpoint: str, output_path: str) -> dict:
    del instrument_endpoint
    resource_manager = pyvisa.ResourceManager("@iab")
    _LEAKS.append(resource_manager)
    for resource_name in resource_manager.list_resources():
        _LEAKS.append(resource_manager.open_resource(resource_name))
    result = {"invalid": "sessions intentionally leaked"}
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    os._exit(0)
