from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evaluators.pyvisa_dut_validation_v1.oracle.reconstruct import reconstruct
from evaluators.pyvisa_dut_validation_v1.worlds import load_world_specs
from evaluators.pyvisa_dut_validation_v2.projection import project_events
from evaluators.pyvisa_dut_validation_v2.tests.test_projection import raw_nominal
from instrument_benchmark_evaluator.container.evidence import ContainerEvidence
from instrument_benchmark_evaluator.container.errors import ContainerInfrastructureError
from instrument_benchmark_evaluator.container.runner import ContainerProcessResult
from instrument_benchmark_evaluator.container.sim_evidence import SimJournalEvidence
from instrument_benchmark_evaluator.container.sim_runner import (
    SimContainerHandle,
    SimContainerResult,
)
from instrument_benchmark_evaluator.contracts import (
    RunSettings,
    load_instance_settings,
)
from instrument_benchmark_evaluator.v2_run import run_v2_full_suite, run_v2_world


ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT.parent / "instance" / "pyvisa_dut_validation_v2"
CANDIDATE = ROOT / "evaluators" / "pyvisa_dut_validation_v2" / "reference" / "solution.py"
WORLD_DIRECTORY = ROOT / "evaluators" / "pyvisa_dut_validation_v1" / "worlds"
FIXED_WORLDS = (
    "nominal",
    "reordered_resources",
    "distractor_devices",
    "numeric_formats",
    "binary_block_variants",
    "delayed_settle",
    "dirty_initial_state",
    "dut_gain_failure",
    "command_error",
)


def container_evidence(role: str) -> ContainerEvidence:
    user = "10001:10001" if role == "candidate" else "11001:11001"
    return ContainerEvidence(
        container_id=f"{role}-container",
        image_digest="sha256:" + ("1" if role == "candidate" else "2") * 64,
        created_at="created",
        started_at="started",
        finished_at="finished",
        status="exited",
        exit_code=0,
        oom_killed=False,
        user=user,
        network_mode="none",
        readonly_rootfs=True,
        cap_drop=("ALL",),
        security_options=("no-new-privileges",),
        memory_bytes=536870912,
        nano_cpus=1000000000,
        pids_limit=64,
        pid_mode="",
        ipc_mode="private",
        uts_mode="",
        mounts=(),
        cleanup_attempted=True,
        cleanup_succeeded=True,
    )


class FakeBackend:
    def __init__(
        self,
        events,
        candidate,
        *,
        status="completed",
        candidate_status=None,
    ) -> None:
        self.events = events
        self.candidate = candidate
        self.status = status
        self.candidate_status = candidate_status or status

    def invoke(self, *, workspace, endpoint, **kwargs):
        self.events.append("candidate")
        self.assertions = (
            workspace.is_dir(),
            endpoint.parent.name == "transport",
            endpoint.name == "visa.sock",
        )
        self.events.append("candidate_removed")
        return ContainerProcessResult(
            status=self.status,
            returncode=0 if self.status == "completed" else 1,
            stdout="",
            stderr="",
            result=self.candidate if self.status == "completed" else None,
            container_evidence=container_evidence("candidate"),
            artifact_evidence=None,
            candidate_status=self.candidate_status,
        )


class FakeSimRunner:
    def __init__(
        self,
        events,
        raw,
        snapshot,
        *,
        fail_start=False,
        fail_finalize=False,
        fatal=None,
    ):
        self.events = events
        self.raw = raw
        self.snapshot = snapshot
        self.fail_start = fail_start
        self.fail_finalize = fail_finalize
        self.fatal = fatal

    def start(self, *, run_id, world_id, world_path, transport_dir, evidence_dir):
        self.events.append("sim_start")
        if self.fail_start:
            raise ContainerInfrastructureError("sim readiness failed")
        self.start_assertions = (
            world_path.is_file(),
            not bool(world_path.stat().st_mode & 0o222),
            transport_dir.is_dir(),
            evidence_dir.is_dir(),
            (world_path.parent / "workspace").is_dir(),
            (world_path.parent / "output").is_dir(),
        )
        return SimContainerHandle(
            "sim-container",
            "sim-name",
            run_id,
            world_id,
            transport_dir / "visa.sock",
            evidence_dir,
            world_path,
        )

    def finalize(self, handle):
        self.events.append("sim_finalize")
        if self.fail_finalize:
            raise ContainerInfrastructureError("bad sim journal")
        events = tuple(event.to_dict() for event in self.raw)
        journal = SimJournalEvidence(
            events=events,
            event_count=len(events),
            final_hash=self.raw[-1].event_hash,
            pre_cleanup_snapshot={**self.snapshot.__dict__},
            post_cleanup_snapshot={
                **self.snapshot.__dict__,
                "closed_routes": (),
                "psu_output": False,
                "awg_output": False,
                "safe": True,
            },
            counts={},
            broker={},
            open_sessions=0,
            leaked_sessions=0,
            safe=True,
            fatal=self.fatal,
        )
        self.events.append("sim_removed")
        return SimContainerResult(
            container_evidence("sim"), journal, self.fatal
        )


class V2WorldRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec, self.raw, self.snapshot, _, _ = raw_nominal()
        projected = project_events(self.raw)
        self.candidate = reconstruct(projected, self.spec).to_candidate_result()
        self.instance = load_instance_settings(
            INSTANCE, expected_evaluator_id="pyvisa_dut_validation_v2"
        )

    def benchmark(self, shared: Path) -> RunSettings:
        return RunSettings(
            instance_path=INSTANCE,
            fixed_worlds=(self.spec.world_id,),
            repeated_worlds=10,
            timeout_seconds=5,
            max_output_bytes=65536,
            run_id="v2-run",
            shared_run_root=shared,
        )

    def test_completed_order_layout_projection_and_schema_two_report(self) -> None:
        events = []
        backend = FakeBackend(events, self.candidate)
        sim = FakeSimRunner(events, self.raw, self.snapshot)
        with tempfile.TemporaryDirectory() as directory:
            execution = run_v2_world(
                benchmark=self.benchmark(Path(directory)),
                instance=self.instance,
                spec=self.spec,
                candidate_path=CANDIDATE,
                backend=backend,
                sim_runner=sim,
            )
        self.assertEqual(
            events,
            [
                "sim_start",
                "candidate",
                "candidate_removed",
                "sim_finalize",
                "sim_removed",
            ],
        )
        self.assertTrue(all(backend.assertions))
        self.assertTrue(all(sim.start_assertions))
        self.assertTrue(execution.report.base.strict_pass)
        value = execution.report.to_dict()
        self.assertIn("candidate_container_evidence", value)
        self.assertIn("sim_container_evidence", value)
        self.assertIn("sim_journal_evidence", value)

    def test_candidate_failure_is_nonretryable_when_sim_evidence_validates(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as directory:
            execution = run_v2_world(
                benchmark=self.benchmark(Path(directory)),
                instance=self.instance,
                spec=self.spec,
                candidate_path=CANDIDATE,
                backend=FakeBackend(events, self.candidate, status="candidate_failure"),
                sim_runner=FakeSimRunner(events, self.raw, self.snapshot),
            )
        self.assertEqual(execution.report.base.status, "candidate_failure")
        self.assertTrue(execution.report.base.infrastructure_valid)
        self.assertFalse(execution.report.base.retry_eligible)

    def test_all_candidate_outcomes_remain_nonretryable_with_valid_sim(self) -> None:
        for status in (
            "candidate_failure",
            "candidate_timeout",
            "candidate_oom",
            "output_limit",
            "invalid_result",
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                execution = run_v2_world(
                    benchmark=self.benchmark(Path(directory)),
                    instance=self.instance,
                    spec=self.spec,
                    candidate_path=CANDIDATE,
                    backend=FakeBackend([], self.candidate, status=status),
                    sim_runner=FakeSimRunner([], self.raw, self.snapshot),
                )
                self.assertEqual(execution.report.base.status, status)
                self.assertTrue(execution.report.base.infrastructure_valid)
                self.assertFalse(execution.report.base.retry_eligible)
                self.assertFalse(execution.report.base.strict_pass)

    def test_sim_start_or_finalize_failure_is_retryable_schema_two(self) -> None:
        for failure in ("start", "finalize"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                events = []
                execution = run_v2_world(
                    benchmark=self.benchmark(Path(directory)),
                    instance=self.instance,
                    spec=self.spec,
                    candidate_path=CANDIDATE,
                    backend=FakeBackend(events, self.candidate),
                    sim_runner=FakeSimRunner(
                        events,
                        self.raw,
                        self.snapshot,
                        fail_start=failure == "start",
                        fail_finalize=failure == "finalize",
                    ),
                )
                self.assertFalse(execution.report.base.infrastructure_valid)
                self.assertTrue(execution.report.base.retry_eligible)
                self.assertFalse(execution.report.base.strict_pass)

    def test_trusted_fatal_and_candidate_cleanup_failure_are_retryable(self) -> None:
        cases = (
            (
                FakeBackend([], self.candidate),
                FakeSimRunner(
                    [],
                    self.raw,
                    self.snapshot,
                    fatal={"failure_kind": "trusted_sim_failure"},
                ),
            ),
            (
                FakeBackend([], self.candidate, status="infrastructure_failure"),
                FakeSimRunner([], self.raw, self.snapshot),
            ),
        )
        for backend, sim in cases:
            with self.subTest(case=backend.status), tempfile.TemporaryDirectory() as directory:
                execution = run_v2_world(
                    benchmark=self.benchmark(Path(directory)),
                    instance=self.instance,
                    spec=self.spec,
                    candidate_path=CANDIDATE,
                    backend=backend,
                    sim_runner=sim,
                )
                self.assertFalse(execution.report.base.infrastructure_valid)
                self.assertTrue(execution.report.base.retry_eligible)

        cleanup = FakeBackend(
            [],
            self.candidate,
            status="infrastructure_failure",
            candidate_status="candidate_failure",
        )
        with tempfile.TemporaryDirectory() as directory:
            execution = run_v2_world(
                benchmark=self.benchmark(Path(directory)),
                instance=self.instance,
                spec=self.spec,
                candidate_path=CANDIDATE,
                backend=cleanup,
                sim_runner=FakeSimRunner([], self.raw, self.snapshot),
            )
        self.assertEqual(
            execution.report.candidate_container_evidence["candidate_status"],
            "candidate_failure",
        )

    def test_bad_protocol_and_leaked_sessions_fail_without_retry(self) -> None:
        cases = (
            raw_nominal(reject=True),
            raw_nominal(extra_scope_leaks=2),
        )
        for spec, raw, snapshot, _, _ in cases:
            projected = project_events(raw)
            candidate = reconstruct(projected, spec).to_candidate_result()
            with self.subTest(world=spec.world_id), tempfile.TemporaryDirectory() as directory:
                execution = run_v2_world(
                    benchmark=self.benchmark(Path(directory)),
                    instance=self.instance,
                    spec=spec,
                    candidate_path=CANDIDATE,
                    backend=FakeBackend([], candidate),
                    sim_runner=FakeSimRunner([], raw, snapshot),
                )
                self.assertTrue(execution.report.base.infrastructure_valid)
                self.assertFalse(execution.report.base.retry_eligible)
                self.assertFalse(execution.report.base.strict_pass)

    def test_formal_suite_contains_exactly_nine_fixed_and_ten_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            benchmark = RunSettings(
                instance_path=INSTANCE,
                fixed_worlds=FIXED_WORLDS,
                repeated_worlds=10,
                timeout_seconds=5,
                max_output_bytes=65536,
                run_id="v2-suite",
                shared_run_root=Path(directory),
            )
            report = run_v2_full_suite(
                benchmark=benchmark,
                instance=self.instance,
                specs=load_world_specs(WORLD_DIRECTORY),
                candidate_path=CANDIDATE,
                backend=FakeBackend([], self.candidate),
                sim_runner=FakeSimRunner([], self.raw, self.snapshot),
                repeated_base_seed=80000,
            )
        self.assertEqual(len(report.base.fixed_reports), 9)
        self.assertEqual(len(report.base.repeated_reports), 10)
        self.assertEqual(len(report.worlds), 19)
        self.assertEqual(report.to_dict()["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
