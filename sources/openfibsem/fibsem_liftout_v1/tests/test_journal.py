from __future__ import annotations

import json
from pathlib import Path

import pytest

from sources.openfibsem.fibsem_liftout_v1.journal import (
    EventJournal,
    JournalError,
    validate_records,
)


def test_journal_hash_chain_detects_mutation() -> None:
    journal = EventJournal("run", "world")
    journal.append("rpc.request", operation="ping")
    journal.append("rpc.result", operation="ping", ok=True)
    records = [event.to_dict() for event in journal.events]
    records[0]["fields"]["operation"] = "checkpoint"

    with pytest.raises(JournalError, match="hash"):
        validate_records(records, "run", "world")


def test_journal_rejects_wrong_identity_sequence_and_truncation() -> None:
    journal = EventJournal("run", "world")
    journal.append("rpc.request", operation="ping")
    journal.append("rpc.result", operation="ping", ok=True)
    records = [event.to_dict() for event in journal.events]

    with pytest.raises(JournalError, match="identity"):
        validate_records(records, "another-run", "world")
    records[1]["sequence"] = 8
    with pytest.raises(JournalError, match="sequence"):
        validate_records(records, "run", "world")
    with pytest.raises(JournalError, match="terminal"):
        validate_records(records[:1], "run", "world", require_terminal=True)


def test_journal_exports_canonical_jsonl_and_summary_atomically(tmp_path: Path) -> None:
    journal = EventJournal("run", "world")
    journal.append("rpc.request", request_id="req-00000001", operation="ping")
    journal.append("run.terminal", outcome="complete")

    jsonl, summary = journal.export(tmp_path)

    lines = jsonl.read_text(encoding="ascii").splitlines()
    assert [json.loads(line)["sequence"] for line in lines] == [1, 2]
    assert lines[0] == json.dumps(json.loads(lines[0]), sort_keys=True, separators=(",", ":"))
    summary_value = json.loads(summary.read_text(encoding="ascii"))
    assert summary_value["head_hash"] == journal.head_hash
    assert summary_value["event_count"] == 2
    assert not list(tmp_path.glob("*.tmp"))
