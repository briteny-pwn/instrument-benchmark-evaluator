import json


def run_experiment(endpoint, output):
    value = {"ok": True}
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    return value
