#!/usr/bin/env python3
"""Refresh or verify the pinned Linux/amd64 Docker Buildx CLI plugin."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "container" / "docker-buildx"
VERSION = "0.30.1"
PACKAGE = "docker-buildx-plugin=0.30.1-1~ubuntu.22.04~jammy"
URL = (
    "https://download.docker.com/linux/ubuntu/dists/jammy/pool/stable/amd64/"
    "docker-buildx-plugin_0.30.1-1~ubuntu.22.04~jammy_amd64.deb"
)
PACKAGE_SHA256 = "c550ca2fcca56836605b58c64c6a89e198bb9f757d8978e4060a82227bda9c98"
BINARY_SHA256 = "a5a4fbd515283ebf05c450bc5b5fabaeeea3f7ac55c322ec310a016005df45a0"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()
    if arguments.verify:
        verify_bundle()
        return 0

    with tempfile.TemporaryDirectory() as directory:
        package_path = Path(directory) / "docker-buildx-plugin.deb"
        with urllib.request.urlopen(URL, timeout=120) as response:
            package_path.write_bytes(response.read())
        package_payload = package_path.read_bytes()
        if _sha256(package_payload) != PACKAGE_SHA256:
            raise RuntimeError("Docker Buildx package digest mismatch")
        payload = _extract_buildx(package_payload)
    if _sha256(payload) != BINARY_SHA256:
        raise RuntimeError("Docker Buildx binary digest mismatch")
    DESTINATION.mkdir(parents=True, exist_ok=True)
    executable = DESTINATION / "docker-buildx"
    executable.write_bytes(payload)
    executable.chmod(0o755)
    manifest = {
        "schema_version": 1,
        "version": VERSION,
        "platform": "linux/amd64",
        "source": URL,
        "package": PACKAGE,
        "package_sha256": PACKAGE_SHA256,
        "buildx_sha256": BINARY_SHA256,
    }
    (DESTINATION / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    verify_bundle()
    return 0


def verify_bundle() -> None:
    executable = DESTINATION / "docker-buildx"
    manifest_path = DESTINATION / "manifest.json"
    try:
        payload = executable.read_bytes()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load Docker Buildx bundle: {exc}") from exc
    expected = {
        "schema_version": 1,
        "version": VERSION,
        "platform": "linux/amd64",
        "source": URL,
        "package": PACKAGE,
        "package_sha256": PACKAGE_SHA256,
        "buildx_sha256": BINARY_SHA256,
    }
    if manifest != expected or _sha256(payload) != BINARY_SHA256:
        raise RuntimeError("Docker Buildx bundle does not match its lock")
    if executable.stat().st_mode & 0o111 == 0:
        raise RuntimeError("Docker Buildx binary is not executable")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _extract_buildx(package: bytes) -> bytes:
    members = _read_ar_members(package)
    data_name = next(
        (name for name in members if name.startswith("data.tar")),
        None,
    )
    if data_name is None:
        raise RuntimeError("Docker Buildx package has no data archive")
    try:
        with tarfile.open(fileobj=io.BytesIO(members[data_name]), mode="r:*") as archive:
            wanted = "./usr/libexec/docker/cli-plugins/docker-buildx"
            member = archive.getmember(wanted)
            extracted = archive.extractfile(member)
            if extracted is None or not member.isfile():
                raise RuntimeError("Docker Buildx package entry is not a file")
            return extracted.read()
    except (KeyError, tarfile.TarError) as exc:
        raise RuntimeError(f"cannot extract Docker Buildx package: {exc}") from exc


def _read_ar_members(payload: bytes) -> dict[str, bytes]:
    if not payload.startswith(b"!<arch>\n"):
        raise RuntimeError("Docker Buildx package is not a Debian archive")
    members: dict[str, bytes] = {}
    offset = 8
    while offset < len(payload):
        header = payload[offset : offset + 60]
        if len(header) != 60 or header[58:] != b"`\n":
            raise RuntimeError("Docker Buildx package has an invalid ar header")
        name = header[:16].decode("ascii").strip().removesuffix("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as exc:
            raise RuntimeError("Docker Buildx package has an invalid member size") from exc
        start = offset + 60
        end = start + size
        if end > len(payload):
            raise RuntimeError("Docker Buildx package member is truncated")
        members[name] = payload[start:end]
        offset = end + (size % 2)
    return members


if __name__ == "__main__":
    raise SystemExit(main())
