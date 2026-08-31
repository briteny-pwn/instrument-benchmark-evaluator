#!/usr/bin/env python3
"""Refresh the pinned Linux/amd64 Docker CLI used by the offline evaluator image."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "container" / "docker-cli"
VERSION = "27.5.1"
URL = (
    "https://download.docker.com/linux/static/stable/x86_64/"
    f"docker-{VERSION}.tgz"
)
ARCHIVE_SHA256 = "4f798b3ee1e0140eab5bf30b0edc4e84f4cdb53255a429dc3bbae9524845d640"


def main() -> int:
    with urllib.request.urlopen(URL, timeout=60) as response:
        archive = response.read()
    if _sha256(archive) != ARCHIVE_SHA256:
        raise RuntimeError("Docker CLI archive digest mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        member = bundle.getmember("docker/docker")
        if not member.isfile() or member.issym() or member.islnk():
            raise RuntimeError("Docker CLI archive member is unsafe")
        extracted = bundle.extractfile(member)
        if extracted is None:
            raise RuntimeError("Docker CLI archive member is missing")
        executable = extracted.read()
    DESTINATION.mkdir(parents=True, exist_ok=True)
    path = DESTINATION / "docker"
    path.write_bytes(executable)
    path.chmod(0o755)
    manifest = {
        "schema_version": 1,
        "version": VERSION,
        "platform": "linux/amd64",
        "source": URL,
        "archive_sha256": ARCHIVE_SHA256,
        "docker_sha256": _sha256(executable),
    }
    (DESTINATION / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return 0


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
