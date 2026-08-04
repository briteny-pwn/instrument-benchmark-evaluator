from __future__ import annotations

import argparse
import hashlib
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


COUNTED_EVENTS = {
    "connection.open": "connections_opened",
    "connection.close": "connections_closed",
    "connection.reject": "connections_rejected",
    "rpc.request": "rpc_requests",
    "rpc.result": "rpc_results",
    "rpc.reject": "rpc_rejections",
    "resource_query.request": "resource_queries",
    "resource_query.result": "resource_query_results",
    "resource_query.reject": "resource_query_rejections",
    "session.open": "sessions_opened",
    "session.close": "sessions_explicitly_closed",
    "session.forced_cleanup": "sessions_forced_closed",
    "session.invalid_access": "session_invalid_accesses",
    "scpi.write": "scpi_writes",
    "scpi.write_result": "scpi_write_results",
    "scpi.read": "scpi_reads",
    "scpi.read_result": "scpi_read_results",
}


def _event_counts(journal: EventJournal) -> dict[str, int]:
    counts = {name: 0 for name in COUNTED_EVENTS.values()}
    for event in journal.events:
        name = COUNTED_EVENTS.get(event.kind)
        if name is not None:
            counts[name] += 1
    return counts


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
        journal.append(
            "lifecycle.configuration",
            world_sha256=hashlib.sha256(world.read_bytes()).hexdigest(),
            simulator_sha256=hashlib.sha256(simulator.read_bytes()).hexdigest(),
        )
        bench = BenchContext.from_world(simulator, spec, journal)
        broker = RemoteVisaBroker(bench, journal)
        if not stop_event.is_set():
            broker.serve_unix(endpoint, stop_event)
        journal.append(
            "lifecycle.signal",
            signal=str(getattr(stop_event, "iab_signal", "EVENT")),
        )
        broker_summary = broker.freeze_and_close()
        broker.raise_if_failed()
        pre_cleanup = bench.snapshot()
        journal.append("cleanup.pre_snapshot", snapshot=asdict(pre_cleanup))
        bench.force_safe()
        post_cleanup = bench.snapshot()
        if not post_cleanup.safe:
            raise RuntimeError("trusted simulator cleanup remained unsafe")
        journal.append("cleanup.post_snapshot", snapshot=asdict(post_cleanup))
        counts = _event_counts(journal)
        open_sessions = (
            counts["sessions_opened"]
            - counts["sessions_explicitly_closed"]
            - counts["sessions_forced_closed"]
        )
        leaked_sessions = counts["sessions_forced_closed"]
        if (
            open_sessions != 0
            or leaked_sessions != broker_summary.leaked_sessions
            or counts["connections_opened"] != broker_summary.connections
            or counts["connections_closed"] != counts["connections_opened"]
            or counts["rpc_requests"]
            != counts["rpc_results"] + counts["rpc_rejections"]
            or counts["resource_queries"]
            != counts["resource_query_results"]
            + counts["resource_query_rejections"]
        ):
            raise RuntimeError("broker evidence accounting is inconsistent")
        terminal_fields = {
            "broker": asdict(broker_summary),
            "counts": counts,
            "open_sessions": open_sessions,
            "leaked_sessions": leaked_sessions,
            "safe": post_cleanup.safe,
            "fatal": None,
        }
        journal.append(
            "lifecycle.summary",
            **terminal_fields,
        )
        journal.append(
            "lifecycle.finalized",
            pre_cleanup_snapshot=asdict(pre_cleanup),
            post_cleanup_snapshot=asdict(post_cleanup),
            **terminal_fields,
        )
        journal.append("lifecycle.exit", code=0, safe=post_cleanup.safe)
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
            "counts": counts,
            "open_sessions": open_sessions,
            "leaked_sessions": leaked_sessions,
            "safe": post_cleanup.safe,
            "fatal": None,
        }
        journal.export(evidence / "events.jsonl")
        _atomic_json(evidence / "summary.json", summary)
        return 0
    except BaseException as exc:
        cleanup_broker: dict[str, Any] | None = None
        cleanup_pre: dict[str, Any] | None = None
        cleanup_post: dict[str, Any] | None = None
        if broker is not None:
            try:
                cleanup_broker = asdict(broker.freeze_and_close())
            except BaseException as cleanup_exc:
                if journal is not None:
                    journal.append(
                        "cleanup.failure",
                        component="broker",
                        exception_type=type(cleanup_exc).__name__,
                        message="trusted broker cleanup failed",
                    )
        if bench is not None:
            try:
                cleanup_pre = asdict(bench.snapshot())
                if journal is not None and not any(
                    event.kind == "cleanup.pre_snapshot"
                    for event in journal.events
                ):
                    journal.append(
                        "cleanup.pre_snapshot", snapshot=cleanup_pre
                    )
                if journal is None or not any(
                    event.kind == "state.force_safe"
                    for event in journal.events
                ):
                    bench.force_safe()
                cleanup_post = asdict(bench.snapshot())
                if journal is not None and not any(
                    event.kind == "cleanup.post_snapshot"
                    for event in journal.events
                ):
                    journal.append(
                        "cleanup.post_snapshot", snapshot=cleanup_post
                    )
                bench.close()
            except BaseException as cleanup_exc:
                if journal is not None:
                    journal.append(
                        "cleanup.failure",
                        component="bench",
                        exception_type=type(cleanup_exc).__name__,
                        message="trusted bench cleanup failed",
                    )
        fatal = {
            "schema_version": 1,
            "run_id": run_id,
            "failure_kind": "trusted_sim_failure",
            "exception_type": type(exc).__name__,
            "message": "trusted simulator failed",
        }
        if journal is not None:
            if not any(
                event.kind == "lifecycle.finalized"
                for event in journal.events
            ):
                journal.append(
                    "lifecycle.finalized",
                    fatal=True,
                    safe=(
                        cleanup_post.get("safe")
                        if cleanup_post is not None
                        else None
                    ),
                    pre_cleanup_snapshot=cleanup_pre,
                    post_cleanup_snapshot=cleanup_post,
                    broker=cleanup_broker,
                )
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
        stop_event.iab_signal = signal.Signals(_signum).name  # type: ignore[attr-defined]
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
