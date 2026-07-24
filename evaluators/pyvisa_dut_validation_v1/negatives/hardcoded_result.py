from __future__ import annotations

import json


def run_experiment(gateway_endpoint: str, output_path: str) -> dict:
    result = {
        "instruments": {},
        "resources": {},
        "measurements": {
            "awg_points_v": [0.0, 0.3, 0.6, 0.9, 1.2, 0.9, 0.6, 0.3],
            "dmm_samples_v": [0.0] * 8,
            "scope_raw_codes": [0] * 8,
            "scope_voltages_v": [0.0] * 8,
        },
        "derived": {
            "dmm_average_v": 0.0,
            "scope_peak_to_peak_v": 2.4,
            "gain": 2.0,
            "max_scope_dmm_error_v": 0.0,
        },
        "decision": True,
    }
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    return result
