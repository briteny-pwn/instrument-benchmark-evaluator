from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "container" / "openfibsem-requirements.lock"
WHEELHOUSE = ROOT / "container" / "openfibsem-wheelhouse"
MANIFEST = WHEELHOUSE / "manifest.json"
SYSTEM_PACKAGES = ROOT / "container" / "fibsem-system-packages"
EXPECTED_SYSTEM_PACKAGES = {
    "gcc-12-base",
    "libatomic1",
    "libbsd0",
    "libedit2",
    "libicu72",
    "libllvm15",
    "libxml2",
    "libz3-4",
}
HASH_LINE = re.compile(r"^\s+--hash=sha256:([0-9a-f]{64})$")
REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\\\s]+) \\$")
GIT_MAX_FILE_BYTES = 100_000_000
WHEEL_PART_BYTES = 90_000_000


def _stored_payload_hash(wheelhouse: Path, filename: str, record: dict) -> tuple[str, int]:
    parts = record.get("parts")
    paths = (
        [wheelhouse / part["filename"] for part in parts]
        if parts is not None
        else [wheelhouse / filename]
    )
    digest = hashlib.sha256()
    size = 0
    for path in paths:
        payload = path.read_bytes()
        digest.update(payload)
        size += len(payload)
    return digest.hexdigest(), size


def _load_vendor_module():
    path = ROOT / "scripts" / "vendor_openfibsem_wheels.py"
    spec = importlib.util.spec_from_file_location("vendor_openfibsem_wheels", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_lock(path: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    package: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        requirement = REQUIREMENT.fullmatch(line)
        if requirement:
            package = requirement.group(1).lower().replace("_", "-")
            continue
        digest = HASH_LINE.fullmatch(line)
        if digest and package is not None:
            records[package] = digest.group(1)
            package = None
            continue
        raise AssertionError(f"invalid lock line: {line!r}")
    assert package is None
    return records


def test_openfibsem_lock_matches_verified_linux_wheelhouse():
    lock = _parse_lock(LOCK)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["python_version"] == "311"
    assert manifest["platform"] == "manylinux_2_28_x86_64"
    assert manifest["source_commit"] == "2ebccb8b9721234ca66bb94de36d0f7cfe047af9"
    assert set(lock) == {
        record["normalized_name"] for record in manifest["files"].values()
    }
    assert len(lock) == len(manifest["files"])

    for filename, record in manifest["files"].items():
        assert filename.endswith(".whl")
        assert record["platform"] in {"any", "manylinux_x86_64"}
        digest, size = _stored_payload_hash(WHEELHOUSE, filename, record)
        assert record["sha256"] == digest
        assert record["bytes"] == size
        assert lock[record["normalized_name"]] == record["sha256"]
        if size > GIT_MAX_FILE_BYTES:
            assert not (WHEELHOUSE / filename).exists()
            assert len(record["parts"]) >= 2
            for part in record["parts"]:
                path = WHEELHOUSE / part["filename"]
                payload = path.read_bytes()
                assert len(payload) <= WHEEL_PART_BYTES
                assert part["bytes"] == len(payload)
                assert part["sha256"] == hashlib.sha256(payload).hexdigest()


def test_fibsem_system_packages_are_hash_locked_amd64_debs():
    manifest = json.loads(
        (SYSTEM_PACKAGES / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["schema_version"] == 1
    assert manifest["distribution"] == "debian-bookworm"
    assert manifest["architecture"] == "amd64"
    assert set(manifest["packages"]) == EXPECTED_SYSTEM_PACKAGES
    assert set(manifest["files"]) == {
        path.name for path in SYSTEM_PACKAGES.glob("*.deb")
    }
    for filename, record in manifest["files"].items():
        payload = (SYSTEM_PACKAGES / filename).read_bytes()
        assert record["sha256"] == hashlib.sha256(payload).hexdigest()
        assert record["bytes"] == len(payload)


def test_vendor_verification_rejects_a_tampered_wheel(tmp_path: Path):
    module = _load_vendor_module()
    destination = tmp_path / "wheelhouse"
    destination.mkdir()
    wheel = destination / "example-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "example-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: example\nVersion: 1.0\n",
        )
    manifest = {
        "schema_version": 1,
        "python_version": "311",
        "platform": "manylinux_2_28_x86_64",
        "source_commit": "2ebccb8b9721234ca66bb94de36d0f7cfe047af9",
        "source_requirements_sha256": "1" * 64,
        "files": {
            wheel.name: {
                "normalized_name": "example",
                "version": "1.0",
                "sha256": "0" * 64,
                "bytes": len(wheel.read_bytes()),
                "platform": "any",
            }
        },
    }
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    lock = tmp_path / "openfibsem-requirements.lock"
    lock_text = "example==1.0 \\\n" + f"    --hash=sha256:{'0' * 64}\n"
    lock.write_text(lock_text, encoding="utf-8")

    with pytest.raises(module.VendorError, match="hash"):
        module.verify_vendor_bundle(
            destination,
            lock,
            source_commit="2ebccb8b9721234ca66bb94de36d0f7cfe047af9",
            platform="manylinux_2_28_x86_64",
            python_version="311",
        )


def test_vendor_verifies_split_wheels_and_rejects_a_tampered_part(tmp_path: Path):
    module = _load_vendor_module()
    destination = tmp_path / "wheelhouse"
    destination.mkdir()
    wheel = destination / "example-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "example-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: example\nVersion: 1.0\n",
        )
        archive.writestr("example/payload.bin", b"x" * 1024)
    records = module._manifest_records(destination)
    module._split_large_wheels(destination, records, max_file_bytes=128)
    manifest = {
        "schema_version": 1,
        "python_version": "311",
        "platform": "manylinux_2_28_x86_64",
        "source_commit": "2ebccb8b9721234ca66bb94de36d0f7cfe047af9",
        "source_requirements_sha256": "1" * 64,
        "files": records,
    }
    (destination / "manifest.json").write_bytes(module._canonical_json(manifest))
    lock = tmp_path / "openfibsem-requirements.lock"
    lock.write_bytes(module._lock_payload(records))

    module.verify_vendor_bundle(
        destination,
        lock,
        source_commit=manifest["source_commit"],
        platform=manifest["platform"],
        python_version=manifest["python_version"],
    )
    part = destination / records[wheel.name]["parts"][0]["filename"]
    part.write_bytes(part.read_bytes() + b"tampered")
    with pytest.raises(module.VendorError, match="hash"):
        module.verify_vendor_bundle(
            destination,
            lock,
            source_commit=manifest["source_commit"],
            platform=manifest["platform"],
            python_version=manifest["python_version"],
        )
def test_vendor_reads_only_top_level_wheel_metadata():
    module = _load_vendor_module()
    setuptools_wheel = next((ROOT / "container" / "wheelhouse").glob("setuptools-*.whl"))

    assert module._wheel_metadata(setuptools_wheel) == ("setuptools", "80.9.0")


def test_dockerfile_installs_optional_openfibsem_without_network():
    text = (ROOT / "container" / "fibsem-evaluator.Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "COPY openfibsem-wheelhouse /build/openfibsem-wheels" in text
    assert "COPY openfibsem-requirements.lock /build/openfibsem-requirements.lock" in text
    assert "COPY runtime-profile.json /build/runtime-profile.json" in text
    assert "COPY fibsem-system-packages /build/fibsem-system-packages" in text
    assert "dpkg -i /build/fibsem-system-packages/*.deb" in text
    assert (
        "DRJIT_LIBLLVM_PATH=/usr/lib/x86_64-linux-gnu/libLLVM-15.so.1" in text
    )
    assert "COPY openfibsem /build/openfibsem" in text
    assert "--no-index --require-hashes" in text
    assert "--no-deps --no-build-isolation /build/openfibsem" in text
    assert (
        "cp -a /build/openfibsem/fibsem/. "
        "/usr/local/lib/python3.11/site-packages/fibsem/"
    ) in " ".join(text.replace("\\", "").split())
    assert "/usr/local/lib/python3.11/site-packages/fibsem/log/data/crosscorrelation" in text
    assert "/usr/local/lib/python3.11/site-packages/fibsem/db" in text
    assert "USER 11001:11001" in text
