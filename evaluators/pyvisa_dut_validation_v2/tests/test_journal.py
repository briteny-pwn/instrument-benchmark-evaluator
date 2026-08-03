from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from evaluators.pyvisa_dut_validation_v2.journal import EventJournal, GENESIS_HASH


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class EventJournalTests(unittest.TestCase):
    def test_events_are_complete_thread_safe_hash_chain(self) -> None:
        journal = EventJournal("run-1", "world-1")
        threads = [
            threading.Thread(
                target=lambda index=index: journal.append("rpc", index=index)
            )
            for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(journal.events), 20)
        previous = GENESIS_HASH
        for sequence, event in enumerate(journal.events, 1):
            self.assertEqual(event.sequence, sequence)
            self.assertEqual(event.previous_hash, previous)
            unsigned = event.to_dict()
            digest = unsigned.pop("event_hash")
            self.assertEqual(hashlib.sha256(canonical(unsigned)).hexdigest(), digest)
            previous = digest

    def test_caller_mutation_isolated_and_export_is_jsonl(self) -> None:
        journal = EventJournal("run-1", "world-1")
        fields = {"state": {"routes": ["1101"]}}
        journal.append("state", **fields)
        fields["state"]["routes"].append("1102")
        self.assertEqual(journal.events[0].fields["state"]["routes"], ["1101"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            journal.export(path)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records, [journal.events[0].to_dict()])


if __name__ == "__main__":
    unittest.main()
