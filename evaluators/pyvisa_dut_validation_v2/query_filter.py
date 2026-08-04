from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence

from pyvisa import errors, rname
from pyvisa.constants import StatusCode


MAX_QUERY_BYTES = 256
QUERY_TIMEOUT_SECONDS = 0.25


@dataclass(frozen=True)
class ResourceQueryRejected(RuntimeError):
    reason: str
    code: int


def filter_resources(
    resources: Sequence[str], query: str
) -> tuple[str, ...]:
    encoded = query.encode("utf-8")
    if len(encoded) > MAX_QUERY_BYTES:
        raise ResourceQueryRejected(
            "query_length", int(StatusCode.error_invalid_expression)
        )
    payload = json.dumps(
        {"resources": list(resources), "query": query},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", __name__, "--worker"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ResourceQueryRejected(
            "query_timeout", int(StatusCode.error_invalid_expression)
        ) from None
    if completed.returncode != 0 or len(completed.stdout) > 64 * 1024:
        raise RuntimeError("trusted resource-query worker failed")
    try:
        result = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "trusted resource-query worker returned invalid data"
        ) from exc
    if not isinstance(result, dict) or set(result) not in (
        {"resources"},
        {"error_code"},
    ):
        raise RuntimeError("trusted resource-query worker returned invalid shape")
    if "error_code" in result:
        code = result["error_code"]
        if isinstance(code, bool) or not isinstance(code, int):
            raise RuntimeError(
                "trusted resource-query worker returned invalid error"
            )
        raise ResourceQueryRejected("invalid_expression", code)
    filtered = result["resources"]
    if not isinstance(filtered, list) or not all(
        isinstance(resource, str) and resource in resources
        for resource in filtered
    ):
        raise RuntimeError("trusted resource-query worker returned invalid resources")
    return tuple(filtered)


def _worker() -> int:
    try:
        value = json.loads(sys.stdin.buffer.read(64 * 1024 + 1))
        if (
            not isinstance(value, dict)
            or set(value) != {"resources", "query"}
            or not isinstance(value["resources"], list)
            or not all(isinstance(item, str) for item in value["resources"])
            or not isinstance(value["query"], str)
        ):
            return 64
        try:
            filtered = rname.filter(value["resources"], value["query"])
            result = {"resources": list(filtered)}
        except errors.VisaIOError as exc:
            result = {"error_code": int(exc.error_code)}
        sys.stdout.write(json.dumps(result, separators=(",", ":")))
        return 0
    except BaseException:
        return 70


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(64)
    raise SystemExit(_worker())
