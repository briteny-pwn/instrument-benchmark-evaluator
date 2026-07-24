from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "instrument_benchmark_evaluator" / "bootstrap.py"


class ContainerBootstrapTests(unittest.TestCase):
    def run_candidate(self, source: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        workspace = root / "workspace"
        output = root / "output"
        runtime = root / "run"
        workspace.mkdir()
        output.mkdir()
        runtime.mkdir()
        solution = workspace / "solution.py"
        solution.write_text(source, encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(BOOTSTRAP),
                str(solution.resolve()),
                str((runtime / "gateway.sock").resolve()),
                str((output / "result.json").resolve()),
                str((output / "return.json").resolve()),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        return completed, output

    def test_valid_result_is_copied_to_private_return_artifact(self) -> None:
        completed, output = self.run_candidate(
            "import json\n"
            "def run_experiment(endpoint, output):\n"
            " value = {'ok': True, 'endpoint': endpoint}\n"
            " open(output, 'w').write(json.dumps(value))\n"
            " return value\n"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads((output / "return.json").read_text()),
            json.loads((output / "result.json").read_text()),
        )

    def test_missing_entrypoint_is_exit_2(self) -> None:
        completed, _ = self.run_candidate("VALUE = 1\n")
        self.assertEqual(completed.returncode, 2)

    def test_candidate_exception_is_exit_1(self) -> None:
        completed, _ = self.run_candidate(
            "def run_experiment(endpoint, output):\n raise RuntimeError('boom')\n"
        )
        self.assertEqual(completed.returncode, 1)

    def test_missing_result_is_exit_3(self) -> None:
        completed, _ = self.run_candidate(
            "def run_experiment(endpoint, output):\n return {'ok': True}\n"
        )
        self.assertEqual(completed.returncode, 3)

    def test_malformed_json_is_exit_3(self) -> None:
        completed, _ = self.run_candidate(
            "def run_experiment(endpoint, output):\n"
            " open(output, 'w').write('{')\n"
            " return {'ok': True}\n"
        )
        self.assertEqual(completed.returncode, 3)

    def test_returned_and_written_mismatch_is_exit_3(self) -> None:
        completed, _ = self.run_candidate(
            "def run_experiment(endpoint, output):\n"
            " open(output, 'w').write('{\"ok\": false}')\n"
            " return {'ok': True}\n"
        )
        self.assertEqual(completed.returncode, 3)


if __name__ == "__main__":
    unittest.main()
