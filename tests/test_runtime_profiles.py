from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_fibsem_cli_import_does_not_require_pyvisa_runtime():
    script = r'''
import importlib.abc
import sys

class RejectPyvisa(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pyvisa" or fullname.startswith("pyvisa."):
            raise ImportError("PyVISA is unavailable in the FIBSEM runtime profile")
        return None

sys.meta_path.insert(0, RejectPyvisa())
import instrument_benchmark_evaluator.cli
assert not any(name == "pyvisa" or name.startswith("pyvisa.") for name in sys.modules)
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
