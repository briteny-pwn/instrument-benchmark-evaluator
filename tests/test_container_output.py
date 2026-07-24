from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from instrument_benchmark_evaluator.container.output import (
    OutputCollectionError,
    collect_result,
)


class ContainerOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, value: str, name: str = "result.json") -> Path:
        path = self.root / name
        path.write_text(value, encoding="utf-8")
        return path

    def test_collects_regular_bounded_json_with_hash(self) -> None:
        path = self.write('{"ok":true}')
        collected = collect_result(self.root, "result.json", 100)
        self.assertEqual(collected.result, {"ok": True})
        self.assertEqual(collected.artifact.size_bytes, path.stat().st_size)
        self.assertEqual(len(collected.artifact.sha256), 64)

    def test_missing_directory_fifo_symlink_and_oversize_are_rejected(self) -> None:
        cases: list[tuple[str, callable]] = [
            ("missing", lambda: None),
            ("directory", lambda: (self.root / "result.json").mkdir()),
            (
                "fifo",
                lambda: os.mkfifo(self.root / "result.json"),
            ),
            (
                "symlink",
                lambda: (self.root / "result.json").symlink_to(
                    self.write("{}", "target.json")
                ),
            ),
            ("oversize", lambda: self.write("x" * 101)),
        ]
        for label, prepare in cases:
            with self.subTest(label=label):
                for child in self.root.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        child.rmdir()
                    else:
                        child.unlink()
                prepare()
                with self.assertRaises(OutputCollectionError):
                    collect_result(self.root, "result.json", 100)

    def test_malformed_non_object_and_extra_files_are_rejected(self) -> None:
        for payload in ("{", "[]"):
            with self.subTest(payload=payload):
                self.write(payload)
                with self.assertRaises(OutputCollectionError):
                    collect_result(self.root, "result.json", 100)
        self.write("{}")
        self.write("secret", "extra.txt")
        with self.assertRaisesRegex(OutputCollectionError, "unexpected"):
            collect_result(self.root, "result.json", 100)

    def test_return_artifact_must_match_public_result(self) -> None:
        self.write('{"ok":true}')
        self.write('{"ok":false}', "return.json")
        with self.assertRaisesRegex(OutputCollectionError, "mismatch"):
            collect_result(
                self.root,
                "result.json",
                100,
                return_filename="return.json",
            )

    def test_expected_owner_and_mode_are_enforced(self) -> None:
        path = self.write("{}")
        path.chmod(0o666)
        with self.assertRaisesRegex(OutputCollectionError, "mode"):
            collect_result(
                self.root,
                "result.json",
                100,
                expected_uid=os.getuid(),
                allowed_modes=(0o600,),
            )
        path.chmod(0o600)
        with self.assertRaisesRegex(OutputCollectionError, "owner"):
            collect_result(
                self.root,
                "result.json",
                100,
                expected_uid=os.getuid() + 1,
                allowed_modes=(0o600,),
            )


if __name__ == "__main__":
    unittest.main()
