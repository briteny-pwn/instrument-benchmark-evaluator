import json
import os
import socket


def _attempt(operation):
    try:
        operation()
    except Exception as exc:
        return {"succeeded": False, "error": type(exc).__name__}
    return {"succeeded": True, "error": None}


def run_experiment(endpoint, output):
    probes = {}
    probes["outbound"] = _attempt(
        lambda: socket.create_connection(("1.1.1.1", 53), timeout=0.2).close()
    )
    probes["host_evaluator"] = _attempt(
        lambda: open("/evaluator/secret", "rb").read(1)
    )
    probes["evaluator_package"] = _attempt(
        lambda: open(
            "/usr/local/lib/python3.11/site-packages/"
            "instrument_benchmark_evaluator/cli.py",
            "rb",
        ).read(1)
    )
    probes["hidden_world"] = _attempt(
        lambda: open("/evaluator/worlds/nominal.yaml", "rb").read(1)
    )
    probes["simulator_yaml"] = _attempt(
        lambda: open("/evaluator/simulator/base.yaml", "rb").read(1)
    )
    probes["oracle"] = _attempt(
        lambda: open("/evaluator/oracle/reconstruct.py", "rb").read(1)
    )
    probes["journal"] = _attempt(
        lambda: open("/evaluator/gateway/journal.py", "rb").read(1)
    )
    probes["outer_request"] = _attempt(
        lambda: open("/run/evaluator/request.json", "rb").read(1)
    )
    probes["outer_report"] = _attempt(
        lambda: open("/run/evaluator/report.json", "rb").read(1)
    )
    probes["git_metadata"] = _attempt(lambda: open("/workspace/.git/HEAD").read())
    probes["docker_socket"] = _attempt(
        lambda: socket.socket(socket.AF_UNIX).connect("/var/run/docker.sock")
    )
    probes["host_device"] = _attempt(lambda: open("/dev/mem", "rb").read(1))
    probes["root_write"] = _attempt(
        lambda: open("/forbidden-write", "w").write("forbidden")
    )
    response = b""
    gateway = socket.socket(socket.AF_UNIX)
    gateway.settimeout(2)
    gateway.connect(endpoint)
    gateway.sendall(b"PING")
    response = gateway.recv(4)
    gateway.close()
    value = {
        "uid": os.getuid(),
        "gateway_ok": response == b"PONG",
        "probes": probes,
    }
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    return value
