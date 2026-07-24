from __future__ import annotations

import unittest

from instrument_benchmark_evaluator.container.evidence import normalize_inspect


class ContainerEvidenceTests(unittest.TestCase):
    def test_normalizes_security_and_runtime_evidence(self) -> None:
        value = {
            "Id": "abc",
            "Image": "sha256:" + "1" * 64,
            "Created": "2026-07-24T00:00:00Z",
            "Config": {"User": "10001:10001"},
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Memory": 536870912,
                "NanoCpus": 1000000000,
                "PidsLimit": 64,
            },
            "State": {
                "Status": "exited",
                "ExitCode": 0,
                "OOMKilled": False,
                "StartedAt": "2026-07-24T00:00:01Z",
                "FinishedAt": "2026-07-24T00:00:02Z",
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/host/run",
                    "Destination": "/run/iab",
                    "Mode": "ro",
                    "RW": False,
                }
            ],
        }
        evidence = normalize_inspect(value)
        self.assertEqual(evidence.network_mode, "none")
        self.assertTrue(evidence.readonly_rootfs)
        self.assertEqual(evidence.user, "10001:10001")
        self.assertEqual(evidence.cap_drop, ("ALL",))
        self.assertEqual(evidence.security_options, ("no-new-privileges",))
        self.assertEqual(evidence.memory_bytes, 536870912)
        self.assertEqual(evidence.nano_cpus, 1000000000)
        self.assertEqual(evidence.pids_limit, 64)
        self.assertEqual(evidence.exit_code, 0)
        self.assertFalse(evidence.oom_killed)
        self.assertFalse(evidence.mounts[0].writable)


if __name__ == "__main__":
    unittest.main()
