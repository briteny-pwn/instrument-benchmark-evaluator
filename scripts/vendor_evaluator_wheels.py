#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WHEELHOUSE = ROOT / "container" / "wheelhouse"
PACKAGES = (
    "python-dotenv==1.2.3",
    "PyYAML==6.0.3",
    "PyVISA==1.16.2",
    "PyVISA-sim==0.7.1",
    "setuptools==80.9.0",
)


def records(directory: Path) -> dict[str, dict[str, int | str]]:
    result: dict[str, dict[str, int | str]] = {}
    for path in sorted(directory.glob("*.whl")):
        payload = path.read_bytes()
        result[path.name] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return result


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="iab-evaluator-wheels-") as directory:
        target = Path(directory)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--dest",
                str(target),
                "--only-binary=:all:",
                "--platform",
                "manylinux_2_17_x86_64",
                "--python-version",
                "311",
                "--implementation",
                "cp",
                "--abi",
                "cp311",
                *PACKAGES,
            ],
            check=True,
        )
        downloaded = records(target)
        manifest_path = WHEELHOUSE / "manifest.json"
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
        if downloaded != expected.get("files"):
            print(json.dumps(downloaded, indent=2, sort_keys=True), file=sys.stderr)
            raise RuntimeError("downloaded evaluator wheels do not match manifest")
    print("evaluator wheelhouse matches manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
