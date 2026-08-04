from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluators.pyvisa_dut_validation_v1.models import WorldSpec
from evaluators.pyvisa_dut_validation_v1.scoring import grade_run
from evaluators.pyvisa_dut_validation_v1.worlds import load_world_specs
from evaluators.pyvisa_dut_validation_v2.journal import JournalEvent
from evaluators.pyvisa_dut_validation_v2.projection import project_events
from evaluators.pyvisa_dut_validation_v2.service import run_service
from evaluators.pyvisa_dut_validation_v2.world_contract import dump_world
from instrument_benchmark_evaluator.container.sim_evidence import verify_evidence
from instrument_benchmark_evaluator.container.evidence import ContainerEvidence
from instrument_benchmark_evaluator.container.runner import ContainerProcessResult
from instrument_benchmark_evaluator.container.sim_runner import (
    SimContainerHandle,
    SimContainerResult,
    _probe_readiness,
)
from instrument_benchmark_evaluator.contracts import (
    RunSettings,
    load_instance_settings,
)
from instrument_benchmark_evaluator.v2_run import _snapshot, run_v2_world


ROOT = Path(__file__).resolve().parents[3]
INSTANCE_PACKAGE = ROOT.parent / "instance" / "pyvisa_dut_validation_v2"
REFERENCE = ROOT / "evaluators" / "pyvisa_dut_validation_v2" / "reference" / "solution.py"
SIMULATOR = ROOT / "evaluators" / "pyvisa_dut_validation_v2" / "simulator.yaml"
WORLDS = ROOT / "evaluators" / "pyvisa_dut_validation_v1" / "worlds"
INSTANCE = ROOT.parent / "instance" / "pyvisa_dut_validation_v2"
BAD_PROTOCOL = (
    ROOT / "evaluators" / "pyvisa_dut_validation_v2" / "negatives" / "bad_protocol.py"
)
LEAKED_SESSIONS = (
    ROOT
    / "evaluators"
    / "pyvisa_dut_validation_v2"
    / "negatives"
    / "leaked_sessions.py"
)


def container_evidence(role: str) -> ContainerEvidence:
    return ContainerEvidence(
        container_id=f"local-{role}",
        image_digest="sha256:" + ("1" if role == "candidate" else "2") * 64,
        created_at="created",
        started_at="started",
        finished_at="finished",
        status="exited",
        exit_code=0,
        oom_killed=False,
        user="10001:10001" if role == "candidate" else "11001:11001",
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


class LocalCandidateBackend:
    def invoke(self, *, workspace, endpoint, instance, timeout_seconds, **kwargs):
        output = workspace.parent / "output" / "result.json"
        environment = dict(os.environ)
        environment["IAB_VISA_SOCKET"] = str(endpoint)
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                (str(INSTANCE), environment.get("PYTHONPATH", "")),
            )
        )
        code = (
            "import importlib.util,sys;"
            "s=importlib.util.spec_from_file_location('candidate',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "m.run_experiment(sys.argv[2],sys.argv[3])"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(workspace / instance.submission_filename),
                str(endpoint),
                str(output),
            ],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        result = json.loads(output.read_text()) if output.is_file() else None
        return ContainerProcessResult(
            status="completed" if completed.returncode == 0 else "candidate_failure",
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            result=result,
            container_evidence=container_evidence("candidate"),
            artifact_evidence=None,
        )


class LocalSimRunner:
    def start(self, *, run_id, world_id, world_path, transport_dir, evidence_dir):
        self.stop = threading.Event()
        self.stop.iab_signal = "SIGTERM"  # type: ignore[attr-defined]
        self.result_codes: list[int] = []
        endpoint = transport_dir / "visa.sock"
        self.worker = threading.Thread(
            target=lambda: self.result_codes.append(
                run_service(
                    world=world_path,
                    endpoint=endpoint,
                    evidence=evidence_dir,
                    simulator=SIMULATOR,
                    run_id=run_id,
                    stop_event=self.stop,
                )
            )
        )
        self.worker.start()
        deadline = time.monotonic() + 3.0
        while not endpoint.exists() and self.worker.is_alive():
            if time.monotonic() >= deadline:
                raise RuntimeError("sim broker did not become ready")
            time.sleep(0.01)
        if not _probe_readiness(endpoint, 1.0):
            raise RuntimeError("sim hello failed")
        return SimContainerHandle(
            "local-sim",
            "local-sim",
            run_id,
            world_id,
            endpoint,
            evidence_dir,
            world_path,
        )

    def finalize(self, handle):
        self.stop.set()
        self.worker.join(5.0)
        if self.worker.is_alive() or self.result_codes != [0]:
            raise RuntimeError(f"local sim failed: {self.result_codes}")
        journal = verify_evidence(
            handle.evidence_dir,
            run_id=handle.run_id,
            world_id=handle.world_id,
        )
        return SimContainerResult(container_evidence("sim"), journal, journal.fatal)


def load_reference():
    spec = importlib.util.spec_from_file_location("v2_reference_solution", REFERENCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load v2 reference solution")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class V2EndToEndTests(unittest.TestCase):
    def test_formal_world_runner_executes_reference_and_real_negatives(self) -> None:
        spec = load_world_specs(WORLDS)["nominal"]
        instance = load_instance_settings(
            INSTANCE, expected_evaluator_id="pyvisa_dut_validation_v2"
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "evaluators.pyvisa_dut_validation_v2.broker._peer_credentials",
            return_value=(10001, 10001, 1),
        ):
            benchmark = RunSettings(
                instance_path=INSTANCE,
                fixed_worlds=("nominal",),
                repeated_worlds=10,
                timeout_seconds=5,
                max_output_bytes=65536,
                run_id="formal-e2e",
                shared_run_root=Path(directory),
            )
            reports = {}
            for candidate in (REFERENCE, BAD_PROTOCOL, LEAKED_SESSIONS):
                execution = run_v2_world(
                    benchmark=benchmark,
                    instance=instance,
                    spec=spec,
                    candidate_path=candidate,
                    backend=LocalCandidateBackend(),
                    sim_runner=LocalSimRunner(),
                )
                reports[candidate.name + candidate.parent.name] = execution.report.base

        reference = reports["solution.pyreference"]
        bad_protocol = reports["bad_protocol.pynegatives"]
        leaked = reports["leaked_sessions.pynegatives"]
        self.assertTrue(reference.strict_pass, reference.errors)
        for report in (bad_protocol, leaked):
            self.assertTrue(report.infrastructure_valid)
            self.assertFalse(report.retry_eligible)
            self.assertFalse(report.strict_pass)
        self.assertFalse(bad_protocol.gates["no_forbidden_access"])
        self.assertFalse(leaked.gates["active_close_all"])

    def test_reference_uses_real_pyvisa_frontend_rpc_broker_and_sim(self) -> None:
        source = REFERENCE.read_text(encoding="utf-8")
        self.assertIn('pyvisa.ResourceManager("@iab")', source)
        self.assertNotIn("pyvisa_sim", source)
        for spec in load_world_specs(WORLDS).values():
            with self.subTest(world=spec.world_id):
                self._run_world(spec)

    def _run_world(self, spec: WorldSpec) -> None:
        stop = threading.Event()
        stop.iab_signal = "SIGTERM"  # type: ignore[attr-defined]
        result_code: list[int] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            world = root / "world.json"
            endpoint = root / "transport" / "visa.sock"
            evidence = root / "evidence"
            output = root / "result.json"
            dump_world(spec, world)

            with patch(
                "evaluators.pyvisa_dut_validation_v2.broker._peer_credentials",
                return_value=(10001, 10001, 1),
            ):
                worker = threading.Thread(
                    target=lambda: result_code.append(
                        run_service(
                            world=world,
                            endpoint=endpoint,
                            evidence=evidence,
                            simulator=SIMULATOR,
                            run_id="reference-e2e",
                            stop_event=stop,
                        )
                    )
                )
                worker.start()
                deadline = time.monotonic() + 3.0
                while not endpoint.exists() and worker.is_alive():
                    if time.monotonic() >= deadline:
                        self.fail("sim broker did not become ready")
                    time.sleep(0.01)
                self.assertTrue(_probe_readiness(endpoint, 1.0))

                sys.path.insert(0, str(INSTANCE_PACKAGE))
                try:
                    try:
                        with patch.dict(
                            os.environ, {"IAB_VISA_SOCKET": str(endpoint)}
                        ):
                            candidate = load_reference().run_experiment(
                                str(endpoint), str(output)
                            )
                    except Exception as exc:
                        stop.set()
                        worker.join(5.0)
                        fatal = evidence / "fatal.json"
                        detail = (
                            fatal.read_text(encoding="utf-8")
                            if fatal.is_file()
                            else ""
                        )
                        self.fail(
                            f"reference failed: {exc}; sim evidence: {detail}"
                        )
                finally:
                    sys.path.remove(str(INSTANCE_PACKAGE))
                    for name in tuple(sys.modules):
                        if name == "pyvisa_iab" or name.startswith("pyvisa_iab."):
                            sys.modules.pop(name, None)
                    from pyvisa.highlevel import VisaLibraryBase

                    VisaLibraryBase._registry.clear()
                    stop.set()
                    worker.join(5.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(result_code, [0])
            journal = verify_evidence(
                evidence, run_id="reference-e2e", world_id=spec.world_id
            )
            projected = project_events(
                tuple(JournalEvent(**event) for event in journal.events)
            )
            report = grade_run(
                candidate,
                projected,
                spec,
                _snapshot(journal.pre_cleanup_snapshot),
                forbidden_access=any(
                    event.operation in {"protocol_reject", "rpc_reject"}
                    for event in projected
                ),
                infrastructure_ok=True,
            )
            self.assertTrue(report.strict_pass, report.errors)
            self.assertTrue(_snapshot(journal.post_cleanup_snapshot).safe)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
