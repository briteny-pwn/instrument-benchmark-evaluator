from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .bench import BenchContext
from .broker import RemoteVisaBroker
from .journal import EventJournal
from .world_contract import load_world


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def run_service(
    *,
    world: Path,
    endpoint: Path,
    evidence: Path,
    simulator: Path,
    run_id: str,
    stop_event: threading.Event,
) -> int:
    evidence.mkdir(parents=True, exist_ok=True)
    journal: EventJournal | None = None
    bench: BenchContext | None = None
    broker: RemoteVisaBroker | None = None
    try:
        spec = load_world(world)
        journal = EventJournal(run_id, spec.world_id)
        journal.append("lifecycle.start", endpoint_name=endpoint.name)
        bench = BenchContext.from_world(simulator, spec, journal)
        broker = RemoteVisaBroker(bench, journal)
        if not stop_event.is_set():
            broker.serve_unix(endpoint, stop_event)
        pre_cleanup = bench.snapshot()
        broker_summary = broker.freeze_and_close()
        bench.force_safe()
        post_cleanup = bench.snapshot()
        journal.append(
            "lifecycle.finalized",
            pre_cleanup_snapshot=asdict(pre_cleanup),
            post_cleanup_snapshot=asdict(post_cleanup),
            broker=asdict(broker_summary),
        )
        bench.close()
        summary = {
            "schema_version": 1,
            "run_id": run_id,
            "world_id": spec.world_id,
            "broker": asdict(broker_summary),
            "pre_cleanup_snapshot": asdict(pre_cleanup),
            "post_cleanup_snapshot": asdict(post_cleanup),
            "event_count": len(journal.events),
            "final_hash": journal.final_hash,
        }
        journal.export(evidence / "events.jsonl")
        _atomic_json(evidence / "summary.json", summary)
        return 0
    except BaseException as exc:
        if broker is not None:
            try:
                broker.freeze_and_close()
            except BaseException:
                pass
        if bench is not None:
            try:
                bench.force_safe()
                bench.close()
            except BaseException:
                pass
        fatal = {
            "schema_version": 1,
            "run_id": run_id,
            "failure_kind": "trusted_sim_failure",
            "exception_type": type(exc).__name__,
            "message": "trusted simulator failed",
        }
        if journal is not None:
            journal.append("trusted.fatal", **fatal)
            journal.export(evidence / "events.jsonl")
            fatal["final_hash"] = journal.final_hash
        _atomic_json(evidence / "fatal.json", fatal)
        return 70


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iab-sim-service")
    parser.add_argument("--world", type=Path, required=True)
    parser.add_argument("--endpoint", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--simulator",
        type=Path,
        default=Path(__file__).with_name("simulator.yaml"),
    )
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    stop_event = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return run_service(
        world=args.world,
        endpoint=args.endpoint,
        evidence=args.evidence,
        simulator=args.simulator,
        run_id=args.run_id,
        stop_event=stop_event,
    )


if __name__ == "__main__":
    raise SystemExit(main())
