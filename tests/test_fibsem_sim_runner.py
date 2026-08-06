from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from evaluators.fibsem_liftout_v1.checkpoint_exporter import CheckpointExporter
from evaluators.fibsem_liftout_v1.journal import EventJournal
from evaluators.fibsem_liftout_v1.models import ScenarioSpec
from evaluators.fibsem_liftout_v1.service import FibsemService
from evaluators.fibsem_liftout_v1.tests.fakes import RecordingBackend
from instrument_benchmark_evaluator.container.fibsem_sim_runner import (
    FibsemSimContainerRunner,
    _create_arguments,
    load_fibsem_evidence,
)
from instrument_benchmark_evaluator.container.docker_client import DockerCommandResult
from tests.test_container_runner import inspect


class FakeDockerClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.value = inspect(0)
        self.value["Id"] = "fibsem-sim"
        self.value["Image"] = "sha256:" + "a" * 64
        self.value["Config"]["User"] = "11001:11001"
        self.value["Config"]["StopTimeout"] = 10
        self.value["HostConfig"].update(
            {
                "Memory": 2 * 1024 * 1024 * 1024,
                "MemorySwap": 2 * 1024 * 1024 * 1024,
                "NanoCpus": 2_000_000_000,
                "PidsLimit": 128,
                "Tmpfs": {
                    "/tmp": "rw,noexec,nosuid,nodev,size=256m,uid=11001,gid=11001"
                },
            }
        )
        self.value["Mounts"] = []

    def run(self, arguments, **kwargs):
        self.calls.append("create")
        for argument in arguments:
            if not argument.startswith("--mount=type=bind,"):
                continue
            fields = dict(
                item.split("=", 1)
                for item in argument.removeprefix("--mount=").split(",")
                if "=" in item
            )
            self.value["Mounts"].append(
                {
                    "Type": "bind",
                    "Source": fields["src"],
                    "Destination": fields["dst"],
                    "Mode": "ro" if "readonly" in argument else "rw",
                    "RW": "readonly" not in argument,
                }
            )
        return DockerCommandResult(0, "fibsem-sim\n", "")

    def start_detached(self, container_id):
        self.calls.append("start")
        self.value["State"]["Status"] = "running"

    def inspect(self, container_id):
        self.calls.append("inspect")
        return self.value

    def signal(self, container_id, signal_name="TERM"):
        self.calls.append("signal")
        self.value["State"]["Status"] = "exited"

    def wait(self, container_id, timeout):
        self.calls.append("wait")
        self.value["State"]["Status"] = "exited"
        return 0

    def remove(self, container_id):
        self.calls.append("remove")


def test_fibsem_runner_mounts_only_transport_evidence_and_hidden_world(
    tmp_path: Path,
) -> None:
    image = "sha256:" + "a" * 64
    transport = (tmp_path / "transport").resolve()
    evidence = (tmp_path / "evidence").resolve()
    world = (tmp_path / "world.json").resolve()

    arguments = _create_arguments(
        image_id=image,
        name="iab-fibsem-sim-run-world",
        run_id="run",
        world_id="world",
        world_path=world,
        transport_dir=transport,
        evidence_dir=evidence,
    )

    assert "--network=none" in arguments
    assert "--read-only" in arguments
    assert "--cap-drop=ALL" in arguments
    assert "--security-opt=no-new-privileges" in arguments
    assert "--user=11001:11001" in arguments
    assert image in arguments
    mounts = [value for value in arguments if value.startswith("--mount=")]
    assert len(mounts) == 3
    assert {"/run/iab/transport", "/run/iab/evidence", "/run/iab/world.json"} == {
        value.split("dst=", 1)[1].split(",", 1)[0] for value in mounts
    }
    assert all("workspace" not in value and "request" not in value for value in mounts)
    assert "serve-fibsem-sim" in arguments
    assert "/run/iab/transport/fibsem.sock" in arguments


def test_fibsem_runner_uses_exact_evaluator_image_not_a_tag(tmp_path: Path) -> None:
    image = "sha256:" + "b" * 64
    arguments = _create_arguments(
        image_id=image,
        name="sim",
        run_id="run",
        world_id="nominal",
        world_path=tmp_path / "world.json",
        transport_dir=tmp_path / "transport",
        evidence_dir=tmp_path / "evidence",
    )

    command_index = arguments.index("serve-fibsem-sim")
    assert arguments[command_index - 1] == image


def test_trusted_evidence_loader_validates_journal_geometry_and_artifacts(
    tmp_path: Path,
) -> None:
    nominal = (
        Path(__file__).resolve().parents[2]
        / "instance"
        / "fibsem_liftout_v1"
        / "scenarios"
        / "nominal.json"
    )
    spec = ScenarioSpec.from_path(nominal)
    journal = EventJournal("run", "nominal")
    backend = RecordingBackend()
    original_state = backend.semantic_state
    backend.semantic_state = lambda: {
        **original_state(),
        "active_operation": None,
        "collision": False,
    }
    service = FibsemService(
        backend,
        spec,
        journal,
        CheckpointExporter(tmp_path),
    )
    service.checkpoint("step_1")
    service.finalize(outcome="candidate_incomplete", forced=True)

    evidence = load_fibsem_evidence(
        tmp_path,
        run_id="run",
        world_id="nominal",
    )

    assert evidence.journal.head_hash == journal.head_hash
    assert tuple(evidence.checkpoints) == ("step_1",)
    assert evidence.checkpoints["step_1"].artifact_complete
    assert evidence.terminal.cleanup_error is None


def test_fibsem_sim_lifecycle_finalizes_evidence_before_removal(tmp_path: Path) -> None:
    image = "sha256:" + "a" * 64
    world = tmp_path / "world.json"
    nominal = (
        Path(__file__).resolve().parents[2]
        / "instance"
        / "fibsem_liftout_v1"
        / "scenarios"
        / "nominal.json"
    )
    world.write_bytes(nominal.read_bytes())
    client = FakeDockerClient()
    runner = FibsemSimContainerRunner(
        client=client,
        evaluator_image_id=image,
        readiness_probe=lambda endpoint, timeout: True,
    )
    handle = runner.start(
        run_id="run",
        world_id="nominal",
        world_path=world,
        transport_dir=tmp_path / "transport",
        evidence_dir=tmp_path / "evidence",
    )
    trusted = object()
    with patch(
        "instrument_benchmark_evaluator.container.fibsem_sim_runner.load_fibsem_evidence",
        return_value=trusted,
    ):
        result = runner.finalize(handle)

    assert result.trusted_evidence is trusted
    assert client.calls.index("wait") < client.calls.index("remove")
    assert result.container_evidence.cleanup_succeeded
