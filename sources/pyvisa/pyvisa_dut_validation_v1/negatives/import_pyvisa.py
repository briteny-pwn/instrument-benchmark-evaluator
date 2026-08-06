from __future__ import annotations

import pyvisa


def run_experiment(gateway_endpoint: str, output_path: str) -> dict:
    return {"backend": pyvisa.ResourceManager("@sim").list_resources()}
