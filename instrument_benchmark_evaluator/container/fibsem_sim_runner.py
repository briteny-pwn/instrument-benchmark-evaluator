from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from sources.openfibsem.fibsem_liftout_v1.geometry.artifacts import (
    ArtifactError,
    validate_checkpoint_bundle,
)
from sources.openfibsem.fibsem_liftout_v1.geometry.oracle import GeometryMetrics
from sources.openfibsem.fibsem_liftout_v1.journal import EventJournal
from sources.openfibsem.fibsem_liftout_v1.journal import canonical_digest
from sources.openfibsem.fibsem_liftout_v1.models import ScenarioSpec
from sources.openfibsem.fibsem_liftout_v1.scoring import (
    CheckpointEvidence,
    TerminalEvidence,
)

from .docker_client import DockerClient
from .errors import ContainerCommandTimeout, ContainerInfrastructureError
from .evidence import ContainerEvidence, normalize_inspect


MAX_SUMMARY_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 32 * 1024 * 1024
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
ReadinessProbe = Callable[[Path, float], bool]


@dataclass(frozen=True)
class FibsemTrustedEvidence:
    journal: EventJournal
    checkpoints: Mapping[str, CheckpointEvidence]
    terminal: TerminalEvidence
    outcome: str
    forced_cleanup: bool
    scenario_digest: str


@dataclass(frozen=True)
class FibsemSimContainerHandle:
    container_id: str
    name: str
    run_id: str
    world_id: str
    endpoint: Path
    evidence_dir: Path
    world_path: Path


@dataclass(frozen=True)
class FibsemSimContainerResult:
    container_evidence: ContainerEvidence
    trusted_evidence: FibsemTrustedEvidence


class FibsemSimContainerRunner:
    def __init__(
        self,
        *,
        client: DockerClient,
        evaluator_image_id: str,
        readiness_probe: ReadinessProbe | None = None,
        readiness_timeout: float = 30.0,
        stop_timeout: float = 20.0,
    ) -> None:
        if not IMAGE_ID.fullmatch(evaluator_image_id):
            raise ValueError("evaluator image ID must be exact")
        self.client = client
        self.evaluator_image_id = evaluator_image_id
        self.readiness_probe = readiness_probe or _probe_readiness
        self.readiness_timeout = readiness_timeout
        self.stop_timeout = stop_timeout

    def start(
        self,
        *,
        run_id: str,
        world_id: str,
        world_path: Path,
        transport_dir: Path,
        evidence_dir: Path,
    ) -> FibsemSimContainerHandle:
        if world_path.is_symlink() or not world_path.is_file():
            raise ContainerInfrastructureError("hidden FIBSEM world file is invalid")
        world_path = world_path.resolve()
        _prepare_empty_directory(transport_dir, "transport", writable=True)
        _prepare_empty_directory(evidence_dir, "evidence", writable=True)
        name = _name(run_id, world_id)
        endpoint = transport_dir.resolve() / "fibsem.sock"
        arguments = _create_arguments(
            image_id=self.evaluator_image_id,
            name=name,
            run_id=run_id,
            world_id=world_id,
            world_path=world_path,
            transport_dir=transport_dir,
            evidence_dir=evidence_dir,
        )
        container_id: str | None = None
        try:
            created = self.client.run(arguments)
            container_id = created.stdout.strip()
            if not container_id or "\n" in container_id:
                raise ContainerInfrastructureError(
                    "docker create did not return FIBSEM sim container ID"
                )
            self.client.start_detached(container_id)
            if not self.readiness_probe(endpoint, self.readiness_timeout):
                state = self.client.inspect(container_id).get("State")
                if isinstance(state, dict) and state.get("Status") == "exited":
                    raise ContainerInfrastructureError(
                        "FIBSEM sim exited before readiness with code "
                        f"{state.get('ExitCode', 'unknown')}"
                    )
                raise ContainerInfrastructureError("FIBSEM sim readiness timed out")
            return FibsemSimContainerHandle(
                container_id,
                name,
                run_id,
                world_id,
                endpoint,
                evidence_dir.resolve(),
                world_path,
            )
        except BaseException as primary_error:
            if container_id is not None:
                try:
                    self.client.remove(container_id)
                except ContainerInfrastructureError as cleanup_error:
                    raise cleanup_error from primary_error
            raise

    def finalize(
        self, handle: FibsemSimContainerHandle
    ) -> FibsemSimContainerResult:
        evidence: ContainerEvidence | None = None
        trusted: FibsemTrustedEvidence | None = None
        primary_error: BaseException | None = None
        cleanup_error: ContainerInfrastructureError | None = None
        exit_code: int | None = None
        try:
            try:
                initial = self.client.inspect(handle.container_id)
                state = initial.get("State")
                running = isinstance(state, dict) and state.get("Status") == "running"
                if running:
                    try:
                        exit_code = self.client.wait(
                            handle.container_id,
                            min(0.5, self.stop_timeout),
                        )
                    except ContainerCommandTimeout:
                        self.client.signal(handle.container_id, "TERM")
                if exit_code is None:
                    exit_code = self.client.wait(
                        handle.container_id,
                        self.stop_timeout,
                    )
                evidence = normalize_inspect(self.client.inspect(handle.container_id))
                _validate_runtime(evidence, self.evaluator_image_id, handle)
                if evidence.status != "exited" or evidence.exit_code != exit_code:
                    raise ContainerInfrastructureError(
                        "FIBSEM sim wait result does not match inspect state"
                    )
                if evidence.oom_killed:
                    raise ContainerInfrastructureError(
                        "FIBSEM sim container was OOM killed"
                    )
            except BaseException as exc:
                primary_error = exc
            try:
                trusted = load_fibsem_evidence(
                    handle.evidence_dir,
                    run_id=handle.run_id,
                    world_id=handle.world_id,
                    scenario_digest=canonical_digest(
                        ScenarioSpec.from_path(handle.world_path).to_dict()
                    ),
                )
            except BaseException as exc:
                if primary_error is None:
                    primary_error = exc
            if primary_error is None and exit_code not in {0, 70}:
                primary_error = ContainerInfrastructureError(
                    f"FIBSEM sim container exited with code {exit_code}"
                )
            if (
                primary_error is None
                and exit_code == 70
                and trusted is not None
                and trusted.outcome != "infrastructure_failure"
            ):
                primary_error = ContainerInfrastructureError(
                    "FIBSEM sim exit code contradicts trusted summary"
                )
        finally:
            try:
                self.client.remove(handle.container_id)
            except ContainerInfrastructureError as exc:
                cleanup_error = exc
            if evidence is not None:
                evidence = replace(
                    evidence,
                    cleanup_attempted=True,
                    cleanup_succeeded=cleanup_error is None,
                    cleanup_error=None if cleanup_error is None else str(cleanup_error),
                )
        if cleanup_error is not None:
            raise cleanup_error
        if primary_error is not None:
            raise primary_error
        assert evidence is not None and trusted is not None
        return FibsemSimContainerResult(evidence, trusted)


def _create_arguments(
    *,
    image_id: str,
    name: str,
    run_id: str,
    world_id: str,
    world_path: Path,
    transport_dir: Path,
    evidence_dir: Path,
) -> list[str]:
    return [
        "create",
        f"--name={name}",
        "--label=iab.managed=true",
        "--label=iab.role=fibsem-sim",
        f"--label=iab.owner={os.environ.get('IAB_CONTAINER_OWNER', run_id)}",
        f"--label=iab.run={run_id}",
        f"--label=iab.world={world_id}",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--log-driver=none",
        "--user=11001:11001",
        "--env=HOME=/tmp",
        "--env=XDG_CACHE_HOME=/tmp/.cache",
        "--cpus=2.0",
        "--memory=2048m",
        "--memory-swap=2048m",
        "--pids-limit=128",
        "--ulimit=nofile=256:256",
        "--stop-timeout=10",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m,uid=11001,gid=11001",
        f"--mount=type=bind,src={Path(transport_dir).resolve()},dst=/run/iab/transport",
        f"--mount=type=bind,src={Path(evidence_dir).resolve()},dst=/run/iab/evidence",
        (
            f"--mount=type=bind,src={Path(world_path).resolve()},"
            "dst=/run/iab/world.json,readonly"
        ),
        image_id,
        "serve-fibsem-sim",
        "--world",
        "/run/iab/world.json",
        "--endpoint",
        "/run/iab/transport/fibsem.sock",
        "--evidence",
        "/run/iab/evidence",
        "--run-id",
        run_id,
    ]


def _validate_runtime(
    evidence: ContainerEvidence,
    image_id: str,
    handle: FibsemSimContainerHandle,
) -> None:
    mounts = {mount.destination: mount for mount in evidence.mounts}
    checks = {
        "image": evidence.image_digest == image_id,
        "user": evidence.user == "11001:11001",
        "network": evidence.network_mode == "none",
        "rootfs": evidence.readonly_rootfs,
        "capabilities": "ALL" in evidence.cap_drop,
        "privileges": "no-new-privileges" in evidence.security_options,
        "memory": evidence.memory_bytes == 2 * 1024 * 1024 * 1024,
        "swap": evidence.memory_swap_bytes == 2 * 1024 * 1024 * 1024,
        "cpu": evidence.nano_cpus == 2_000_000_000,
        "pids": evidence.pids_limit == 128,
        "logs": evidence.log_driver == "none",
        "nofile": evidence.ulimits == ("nofile:256:256",),
        "stop timeout": evidence.stop_timeout == 10,
        "tmpfs": evidence.tmpfs
        == (
            "/tmp:rw,noexec,nosuid,nodev,size=256m,uid=11001,gid=11001",
        ),
        "pid namespace": evidence.pid_mode == "",
        "ipc namespace": evidence.ipc_mode in {"", "private"},
        "uts namespace": evidence.uts_mode == "",
        "mount allowlist": set(mounts)
        == {"/run/iab/transport", "/run/iab/evidence", "/run/iab/world.json"},
        "transport writable": mounts.get("/run/iab/transport") is not None
        and mounts["/run/iab/transport"].mount_type == "bind"
        and mounts["/run/iab/transport"].writable
        and Path(mounts["/run/iab/transport"].source).resolve()
        == handle.endpoint.parent.resolve(),
        "evidence writable": mounts.get("/run/iab/evidence") is not None
        and mounts["/run/iab/evidence"].mount_type == "bind"
        and mounts["/run/iab/evidence"].writable
        and Path(mounts["/run/iab/evidence"].source).resolve()
        == handle.evidence_dir.resolve(),
        "world read-only": mounts.get("/run/iab/world.json") is not None
        and mounts["/run/iab/world.json"].mount_type == "bind"
        and not mounts["/run/iab/world.json"].writable,
        "world source": mounts.get("/run/iab/world.json") is not None
        and Path(mounts["/run/iab/world.json"].source).resolve()
        == handle.world_path.resolve(),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ContainerInfrastructureError(
            "FIBSEM sim runtime policy mismatch: " + ", ".join(failed)
        )


def _probe_readiness(endpoint: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if stat.S_ISSOCK(endpoint.lstat().st_mode):
                return True
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    return False


def _name(run_id: str, world_id: str) -> str:
    raw = f"iab-fibsem-sim-{run_id}-{world_id}-{secrets.token_hex(6)}"
    return "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in raw
    )[:128]


def _prepare_empty_directory(path: Path, label: str, *, writable: bool) -> None:
    if path.is_symlink():
        raise ContainerInfrastructureError(f"FIBSEM sim {label} directory is invalid")
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise ContainerInfrastructureError(
                f"FIBSEM sim {label} directory must be empty"
            )
    else:
        path.mkdir(parents=True, exist_ok=False)
    path.chmod(0o777 if writable else 0o755)


def load_fibsem_evidence(
    root: Path,
    *,
    run_id: str,
    world_id: str,
    scenario_digest: str | None = None,
) -> FibsemTrustedEvidence:
    evidence_root = Path(root).resolve()
    if not evidence_root.is_dir() or evidence_root.is_symlink():
        raise ValueError("FIBSEM evidence root is invalid")
    summary = _json_object(
        evidence_root / "service-summary.json",
        MAX_SUMMARY_BYTES,
        "service summary",
    )
    required = {
        "schema_version",
        "run_id",
        "world_id",
        "scenario_digest",
        "outcome",
        "checkpoints",
        "checkpoint_evidence",
        "journal",
        "cleanup",
    }
    if set(summary) != required or summary["schema_version"] != 1:
        raise ValueError("FIBSEM service summary fields are invalid")
    if summary["run_id"] != run_id or summary["world_id"] != world_id:
        raise ValueError("FIBSEM service summary identity mismatch")
    summary_scenario_digest = summary["scenario_digest"]
    if not _digest(summary_scenario_digest) or (
        scenario_digest is not None and summary_scenario_digest != scenario_digest
    ):
        raise ValueError("FIBSEM service scenario digest mismatch")
    outcome = summary["outcome"]
    if outcome not in {
        "completed",
        "candidate_incomplete",
        "candidate_failure",
        "infrastructure_failure",
        "cleanup_failure",
    }:
        raise ValueError("FIBSEM service outcome is invalid")
    records = _journal_records(evidence_root / "journal.jsonl")
    journal = EventJournal.from_records(
        records,
        run_id=run_id,
        world_id=world_id,
        require_terminal=True,
    )
    journal_summary = summary["journal"]
    exported_journal_summary = _json_object(
        evidence_root / "journal-summary.json",
        MAX_SUMMARY_BYTES,
        "journal summary",
    )
    if (
        not isinstance(journal_summary, dict)
        or set(journal_summary)
        != {"path", "summary_path", "head_hash", "event_count"}
        or journal_summary["path"] != "journal.jsonl"
        or journal_summary["summary_path"] != "journal-summary.json"
        or journal_summary["head_hash"] != journal.head_hash
        or journal_summary["event_count"] != journal.sequence
        or exported_journal_summary
        != {
            "schema_version": 1,
            "run_id": run_id,
            "world_id": world_id,
            "event_count": journal.sequence,
            "head_hash": journal.head_hash,
        }
    ):
        raise ValueError("FIBSEM journal summary is inconsistent")
    checkpoint_ids = summary["checkpoints"]
    checkpoint_values = summary["checkpoint_evidence"]
    if (
        not isinstance(checkpoint_ids, list)
        or any(not isinstance(step, str) for step in checkpoint_ids)
        or len(set(checkpoint_ids)) != len(checkpoint_ids)
        or not isinstance(checkpoint_values, dict)
        or set(checkpoint_values) != set(checkpoint_ids)
    ):
        raise ValueError("FIBSEM checkpoint index is invalid")
    expected_prefix = ["step_1", "step_2", "step_3", "step_4"][: len(checkpoint_ids)]
    if checkpoint_ids != expected_prefix:
        raise ValueError("FIBSEM checkpoints are out of order")
    _validate_evidence_tree(evidence_root, world_id, checkpoint_ids)
    checkpoints: dict[str, CheckpointEvidence] = {}
    for step_id in checkpoint_ids:
        raw = checkpoint_values[step_id]
        if not isinstance(raw, dict) or set(raw) != {
            "geometry",
            "artifact_digest",
            "artifact_path",
        }:
            raise ValueError(f"FIBSEM checkpoint summary is invalid: {step_id}")
        expected_path = f"artifacts/{world_id}/{step_id}"
        if raw["artifact_path"] != expected_path:
            raise ValueError(f"FIBSEM checkpoint path is invalid: {step_id}")
        geometry = _geometry_metrics(raw["geometry"])
        artifact = validate_checkpoint_bundle(
            evidence_root / expected_path,
            expected_world=world_id,
            expected_step=step_id,
        )
        manifest = _json_object(
            evidence_root / expected_path / "checkpoint.json",
            MAX_SUMMARY_BYTES,
            f"checkpoint {step_id}",
        )
        if (
            raw["artifact_digest"] != artifact.bundle_sha256
            or artifact.geometry_hash != geometry.canonical_geometry_hash
            or manifest.get("scenario_digest") != summary_scenario_digest
            or manifest.get("geometry") != raw["geometry"]
        ):
            raise ArtifactError(f"FIBSEM checkpoint evidence mismatch: {step_id}")
        checkpoints[step_id] = CheckpointEvidence(
            step_id,
            geometry,
            True,
            artifact.bundle_sha256,
        )
    cleanup = summary["cleanup"]
    if not isinstance(cleanup, dict) or set(cleanup) != {
        "forced",
        "pre_cleanup",
        "post_cleanup",
        "error_type",
    }:
        raise ValueError("FIBSEM cleanup evidence is invalid")
    pre = cleanup["pre_cleanup"]
    if not isinstance(pre, dict):
        raise ValueError("FIBSEM pre-cleanup state is invalid")
    inserted = pre.get("inserted")
    active = pre.get("active_operation")
    collision = pre.get("collision")
    if not isinstance(inserted, bool) or not isinstance(collision, bool):
        raise ValueError("FIBSEM terminal state flags are invalid")
    if active is not None and not isinstance(active, str):
        raise ValueError("FIBSEM terminal operation state is invalid")
    cleanup_error = cleanup["error_type"]
    if cleanup_error is not None and not isinstance(cleanup_error, str):
        raise ValueError("FIBSEM cleanup error is invalid")
    forced = cleanup["forced"]
    if not isinstance(forced, bool):
        raise ValueError("FIBSEM cleanup mode is invalid")
    terminal = TerminalEvidence(
        safe=not inserted and active is None,
        simulator_idle=active is None,
        collision=collision,
        cleanup_error=cleanup_error,
    )
    assert isinstance(summary_scenario_digest, str)
    return FibsemTrustedEvidence(
        journal,
        checkpoints,
        terminal,
        outcome,
        forced,
        summary_scenario_digest,
    )


def _geometry_metrics(value: object) -> GeometryMetrics:
    fields = {
        "sample_to_source",
        "sample_to_needle",
        "sample_to_target",
        "needle_joint_section_um",
        "target_joint_section_um",
        "sample_component_count",
        "total_sample_fraction",
        "retained_sample_fraction",
        "sample_position_error_um",
        "sample_orientation_error_degrees",
        "sample_integrity_step_1",
        "sample_integrity_final",
        "changes_within_work_envelopes",
        "envelope_violations",
        "needle_retraction_distance_um",
        "needle_safely_retracted",
        "collision",
        "simulator_idle",
        "canonical_geometry_hash",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("FIBSEM geometry metrics are invalid")
    digest = value["canonical_geometry_hash"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("FIBSEM geometry hash is invalid")
    booleans = {
        "sample_to_source",
        "sample_to_needle",
        "sample_to_target",
        "sample_integrity_step_1",
        "sample_integrity_final",
        "changes_within_work_envelopes",
        "needle_safely_retracted",
        "collision",
        "simulator_idle",
    }
    if any(not isinstance(value[name], bool) for name in booleans):
        raise ValueError("FIBSEM geometry flags are invalid")
    numbers = {
        "needle_joint_section_um",
        "target_joint_section_um",
        "total_sample_fraction",
        "retained_sample_fraction",
        "sample_position_error_um",
        "sample_orientation_error_degrees",
        "needle_retraction_distance_um",
    }
    if any(
        isinstance(value[name], bool)
        or not isinstance(value[name], (int, float))
        or not math.isfinite(float(value[name]))
        for name in numbers
    ):
        raise ValueError("FIBSEM geometry values are invalid")
    components = value["sample_component_count"]
    if isinstance(components, bool) or not isinstance(components, int) or components < 0:
        raise ValueError("FIBSEM sample component count is invalid")
    violations = value["envelope_violations"]
    if not isinstance(violations, list) or any(
        not isinstance(item, str) for item in violations
    ):
        raise ValueError("FIBSEM envelope violations are invalid")
    copied = dict(value)
    copied["envelope_violations"] = tuple(violations)
    return GeometryMetrics(**copied)  # type: ignore[arg-type]


def _journal_records(path: Path) -> list[dict[str, object]]:
    payload = _regular_bytes(path, MAX_JOURNAL_BYTES, "journal")
    records: list[dict[str, object]] = []
    for line in payload.splitlines():
        if not line:
            raise ValueError("FIBSEM journal contains an empty record")
        try:
            value = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except json.JSONDecodeError as exc:
            raise ValueError("FIBSEM journal JSON is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("FIBSEM journal record is invalid")
        records.append(value)
    if not records:
        raise ValueError("FIBSEM journal is empty")
    return records


def _json_object(path: Path, limit: int, label: str) -> dict[str, object]:
    payload = _regular_bytes(path, limit, label)
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"FIBSEM {label} JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"FIBSEM {label} must be an object")
    return value


def _regular_bytes(path: Path, limit: int, label: str) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"FIBSEM {label} file is invalid")
    if info.st_size <= 0 or info.st_size > limit:
        raise ValueError(f"FIBSEM {label} size is invalid")
    return path.read_bytes()


def _validate_evidence_tree(
    root: Path, world_id: str, checkpoint_ids: list[object]
) -> None:
    expected_top = {
        "service-summary.json",
        "journal.jsonl",
        "journal-summary.json",
    }
    if checkpoint_ids:
        expected_top.add("artifacts")
    actual_top = {path.name for path in root.iterdir()}
    if actual_top != expected_top:
        raise ValueError("FIBSEM evidence root contains unexpected entries")
    for filename in expected_top - {"artifacts"}:
        path = root / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"FIBSEM evidence file is invalid: {filename}")
    if not checkpoint_ids:
        return
    artifacts = root / "artifacts"
    world = artifacts / world_id
    if (
        artifacts.is_symlink()
        or world.is_symlink()
        or not artifacts.is_dir()
        or not world.is_dir()
        or {path.name for path in artifacts.iterdir()} != {world_id}
        or {path.name for path in world.iterdir()} != set(checkpoint_ids)
    ):
        raise ValueError("FIBSEM artifact directory index is invalid")


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")
