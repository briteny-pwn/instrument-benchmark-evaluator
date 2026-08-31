from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import struct
import subprocess
import sys
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTRUMENT_ROOT = Path(
    os.environ.get("IAB_INSTRUMENT_ROOT", ROOT.parent / "instrument")
).resolve()
VALIDATOR = ROOT / "scripts" / "validate_fibsem_benchmark.py"


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_fibsem_benchmark", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validator_declares_exact_four_by_ten_acceptance_contract() -> None:
    module = load_validator()

    assert module.EXPECTED_WORLDS == (
        "nominal",
        "small",
        "large",
        "needle_offset",
        "target_pose",
        "seeded_01",
        "seeded_02",
        "seeded_03",
        "seeded_04",
        "seeded_05",
    )
    assert module.EXPECTED_STEPS == ("step_1", "step_2", "step_3", "step_4")


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    pixels = b"".join(b"\x00" + b"\x7f" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _glb() -> bytes:
    payload = b'{"asset":{"version":"2.0"}}   '
    return (
        struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(payload))
        + struct.pack("<I4s", len(payload), b"JSON")
        + payload
    )


def test_validator_parses_and_hashes_complete_checkpoint_bundle(tmp_path: Path) -> None:
    module = load_validator()
    bundle = tmp_path / "step_1"
    components = bundle / "components"
    components.mkdir(parents=True)
    stl = b"trusted".ljust(80, b"\0") + struct.pack("<I", 1) + b"\0" * 50
    payloads = {
        "scene.glb": _glb(),
        "scene.stl": stl,
        "sem.png": _png(2, 2),
        "fib.png": _png(2, 2),
        "components/source.stl": stl,
        "components/sample.stl": stl,
        "components/needle.stl": stl,
        "components/target.stl": stl,
        "components/deposition.stl": stl,
    }
    for relative, payload in payloads.items():
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    checkpoint = {
        "schema_version": 1,
        "world_id": "nominal",
        "step_id": "step_1",
        "geometry_hash": "a" * 64,
        "artifacts": {
            name: {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in sorted(payloads.items())
        },
    }
    (bundle / "checkpoint.json").write_text(json.dumps(checkpoint, sort_keys=True))

    projection = module.validate_checkpoint_bundle(
        bundle,
        world_id="nominal",
        step_id="step_1",
        expected_digest=module.bundle_digest(bundle),
    )

    assert projection == {
        "geometry": "a" * 64,
        "sem": checkpoint["artifacts"]["sem.png"]["sha256"],
        "fib": checkpoint["artifacts"]["fib.png"]["sha256"],
    }
    (bundle / "sem.png").write_bytes(payloads["sem.png"] + b"tampered")
    with unittest.TestCase().assertRaisesRegex(module.ValidationError, "hash"):
        module.validate_checkpoint_bundle(
            bundle,
            world_id="nominal",
            step_id="step_1",
            expected_digest=module.bundle_digest(bundle),
        )


def test_validator_distinguishes_outer_image_id_from_sibling_image_digest() -> None:
    module = load_validator()
    image = "sha256:" + "b" * 64
    evidence = {
        "user": "11001:11001",
        "image_id": image,
        "network_mode": "none",
        "readonly_rootfs": True,
        "cap_drop": ["ALL"],
        "security_options": ["no-new-privileges"],
        "cleanup_succeeded": True,
    }

    module._container_policy(
        evidence,
        user="11001:11001",
        image_digest=image,
        image_field="image_id",
        label="outer evaluator",
    )


@unittest.skipUnless(
    os.environ.get("IAB_RUN_FIBSEM_DOCKER_TESTS") == "1"
    and sys.platform.startswith("linux"),
    "distributed FIBSEM acceptance requires native Linux Docker",
)
class DistributedFibsemLinuxTests(unittest.TestCase):
    def test_validator_runs_reference_twice_without_managed_leaks(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                "--instrument-root",
                str(INSTRUMENT_ROOT),
                "--config",
                str(
                    INSTRUMENT_ROOT
                    / "configs"
                    / "openfibsem"
                    / "fibsem_liftout_v1.yaml"
                ),
            ],
            cwd=INSTRUMENT_ROOT,
            env={**os.environ, "EVALUATOR_REPO_PATH": str(ROOT)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=3600,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout[-16000:])
        self.assertIn('"strict_pass": true', completed.stdout)
        stale = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=iab.managed=true",
                "--format",
                "{{.ID}}",
            ],
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        )
        self.assertFalse(stale.stdout.strip())


if __name__ == "__main__":
    unittest.main()
