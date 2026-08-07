from __future__ import annotations

import unittest

from sources.pyvisa.pyvisa_dut_validation_v2.query_filter import (
    QUERY_TIMEOUT_SECONDS,
)


class ResourceQueryFilterTests(unittest.TestCase):
    def test_worker_timeout_allows_cold_start_margin_and_remains_bounded(self) -> None:
        self.assertGreaterEqual(QUERY_TIMEOUT_SECONDS, 0.75)
        self.assertLessEqual(QUERY_TIMEOUT_SECONDS, 1.0)


if __name__ == "__main__":
    unittest.main()
