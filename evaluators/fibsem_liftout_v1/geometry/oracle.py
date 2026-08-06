from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Mapping

from ..models import ScenarioSpec, vec3
from .connectivity import ContactMetrics, contact_metrics
from .metrics import Bounds, MeshPart, SceneSnapshot


PURPOSE_PHASES = {
    "preflight_cut": "preflight",
    "preflight_deposition": "preflight",
    "protection": "step_1",
    "trench": "step_1",
    "polish": "step_1",
    "u_cut": "step_1",
    "needle_joint": "step_2",
    "source_separation": "step_2",
    "target_joint": "step_3",
    "needle_separation": "step_4",
}
INVALID_DISTANCE_UM = 1.0e12


@dataclass(frozen=True)
class GeometryMetrics:
    sample_to_source: bool
    sample_to_needle: bool
    sample_to_target: bool
    needle_joint_section_um: float
    target_joint_section_um: float
    sample_component_count: int
    total_sample_fraction: float
    retained_sample_fraction: float
    sample_position_error_um: float
    sample_orientation_error_degrees: float
    sample_integrity_step_1: bool
    sample_integrity_final: bool
    changes_within_work_envelopes: bool
    envelope_violations: tuple[str, ...]
    needle_retraction_distance_um: float
    needle_safely_retracted: bool
    collision: bool
    simulator_idle: bool
    canonical_geometry_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_to_source": self.sample_to_source,
            "sample_to_needle": self.sample_to_needle,
            "sample_to_target": self.sample_to_target,
            "needle_joint_section_um": self.needle_joint_section_um,
            "target_joint_section_um": self.target_joint_section_um,
            "sample_component_count": self.sample_component_count,
            "total_sample_fraction": self.total_sample_fraction,
            "retained_sample_fraction": self.retained_sample_fraction,
            "sample_position_error_um": self.sample_position_error_um,
            "sample_orientation_error_degrees": self.sample_orientation_error_degrees,
            "sample_integrity_step_1": self.sample_integrity_step_1,
            "sample_integrity_final": self.sample_integrity_final,
            "changes_within_work_envelopes": self.changes_within_work_envelopes,
            "envelope_violations": list(self.envelope_violations),
            "needle_retraction_distance_um": self.needle_retraction_distance_um,
            "needle_safely_retracted": self.needle_safely_retracted,
            "collision": self.collision,
            "simulator_idle": self.simulator_idle,
            "canonical_geometry_hash": self.canonical_geometry_hash,
        }


class GeometryOracle:
    def __init__(self, scenario: ScenarioSpec):
        self.scenario = scenario
        self.epsilon_um = min(0.05, scenario.tolerances.joint_scale_um * 0.20)

    def evaluate(self, snapshot: SceneSnapshot) -> GeometryMetrics:
        contacts = self._contact_graph(snapshot.parts)
        connectivity = {
            role: self._roles_connected(snapshot.parts, contacts, "sample", role)
            for role in ("source", "needle", "target")
        }
        needle_joint = self._joint_section(
            snapshot.parts, "needle_joint", "sample", "needle"
        )
        target_joint = self._joint_section(
            snapshot.parts, "target_joint", "sample", "target"
        )
        sample_components = self._sample_component_volumes(snapshot.parts, contacts)
        total_sample = sum(sample_components)
        principal_sample = max(sample_components, default=0.0)
        retained = principal_sample / snapshot.planned_sample_volume_um3
        total_fraction = total_sample / snapshot.planned_sample_volume_um3
        envelope_violations = self._envelope_violations(snapshot)
        target_position = self.scenario.world_position("target_pose")
        sample_pose = snapshot.poses.get("sample")
        if sample_pose is None:
            position_error = INVALID_DISTANCE_UM
            orientation_error = INVALID_DISTANCE_UM
        else:
            position_error = _norm(
                tuple(
                    actual - expected
                    for actual, expected in zip(
                        sample_pose.position_um, target_position, strict=True
                    )
                )
            )
            target_orientation = self.scenario.frames["target_pose"].orientation_degrees
            orientation_error = _norm(
                tuple(
                    _angle_delta(actual, expected)
                    for actual, expected in zip(
                        sample_pose.orientation_degrees,
                        target_orientation,
                        strict=True,
                    )
                )
            )
        needle_pose = snapshot.poses.get("needle")
        if sample_pose is None or needle_pose is None:
            retraction_distance = (
                INVALID_DISTANCE_UM if not snapshot.needle_inserted else 0.0
            )
        else:
            retraction_distance = _norm(
                tuple(
                    needle - sample
                    for needle, sample in zip(
                        needle_pose.position_um, sample_pose.position_um, strict=True
                    )
                )
            )
        return GeometryMetrics(
            sample_to_source=connectivity["source"],
            sample_to_needle=connectivity["needle"],
            sample_to_target=connectivity["target"],
            needle_joint_section_um=needle_joint,
            target_joint_section_um=target_joint,
            sample_component_count=len(sample_components),
            total_sample_fraction=total_fraction,
            retained_sample_fraction=retained,
            sample_position_error_um=position_error,
            sample_orientation_error_degrees=orientation_error,
            sample_integrity_step_1=retained >= 0.75,
            sample_integrity_final=retained >= 0.65,
            changes_within_work_envelopes=not envelope_violations,
            envelope_violations=envelope_violations,
            needle_retraction_distance_um=retraction_distance,
            needle_safely_retracted=(
                not snapshot.needle_inserted
                and retraction_distance >= self.scenario.tolerances.safe_retraction_um
            ),
            collision=snapshot.collision,
            simulator_idle=not snapshot.active_operation,
            canonical_geometry_hash=_canonical_geometry_hash(snapshot.parts),
        )

    @staticmethod
    def _sample_component_volumes(
        parts: tuple[MeshPart, ...], graph: dict[int, set[int]]
    ) -> tuple[float, ...]:
        sample_indices = {
            index for index, part in enumerate(parts) if part.role == "sample"
        }
        remaining = set(sample_indices)
        volumes: list[float] = []
        while remaining:
            start = remaining.pop()
            pending = [start]
            component = {start}
            while pending:
                current = pending.pop()
                for neighbour in graph[current] & sample_indices - component:
                    component.add(neighbour)
                    remaining.discard(neighbour)
                    pending.append(neighbour)
            volumes.append(sum(parts[index].mesh.volume_um3 for index in component))
        return tuple(sorted(volumes, reverse=True))

    def _envelope_violations(self, snapshot: SceneSnapshot) -> tuple[str, ...]:
        violations: list[str] = []
        for change in snapshot.material_changes:
            phase = PURPOSE_PHASES.get(change.purpose)
            if phase is None or not self._work_envelope(phase).contains(change.bounds):
                violations.append(change.purpose)
        return tuple(violations)

    def _work_envelope(self, phase: str) -> Bounds:
        envelopes = self.scenario.data["work_envelopes"]
        if not isinstance(envelopes, Mapping):
            raise ValueError("scenario work envelopes are invalid")
        value = envelopes[phase]
        if not isinstance(value, Mapping):
            raise ValueError(f"scenario work envelope {phase} is invalid")
        frame = value.get("frame")
        if not isinstance(frame, str):
            raise ValueError(f"scenario work envelope {phase} frame is invalid")
        center = vec3(value.get("center_um"), f"{phase} envelope center")
        size = vec3(value.get("size_um"), f"{phase} envelope size")
        if min(size) <= 0:
            raise ValueError(f"scenario work envelope {phase} size is invalid")
        origin = self.scenario.world_position(frame)
        world_center = tuple(
            base + offset for base, offset in zip(origin, center, strict=True)
        )
        return Bounds(
            tuple(center_value - extent / 2 for center_value, extent in zip(world_center, size, strict=True)),  # type: ignore[arg-type]
            tuple(center_value + extent / 2 for center_value, extent in zip(world_center, size, strict=True)),  # type: ignore[arg-type]
        )

    def _contact_graph(
        self, parts: tuple[MeshPart, ...]
    ) -> dict[int, set[int]]:
        graph = {index: set() for index in range(len(parts))}
        minimum = min(self.epsilon_um, self.scenario.tolerances.joint_scale_um * 0.25)
        for left in range(len(parts)):
            for right in range(left + 1, len(parts)):
                contact = contact_metrics(
                    parts[left].mesh,
                    parts[right].mesh,
                    epsilon_um=self.epsilon_um,
                    min_section_um=minimum,
                )
                if contact.connected:
                    graph[left].add(right)
                    graph[right].add(left)
        return graph

    @staticmethod
    def _roles_connected(
        parts: tuple[MeshPart, ...],
        graph: dict[int, set[int]],
        first_role: str,
        second_role: str,
    ) -> bool:
        starts = [index for index, part in enumerate(parts) if part.role == first_role]
        targets = {index for index, part in enumerate(parts) if part.role == second_role}
        pending, visited = list(starts), set(starts)
        while pending:
            current = pending.pop()
            if current in targets:
                return True
            for neighbour in graph[current] - visited:
                visited.add(neighbour)
                pending.append(neighbour)
        return False

    def _joint_section(
        self,
        parts: tuple[MeshPart, ...],
        purpose: str,
        first_role: str,
        second_role: str,
    ) -> float:
        best = 0.0
        first = [part for part in parts if part.role == first_role]
        second = [part for part in parts if part.role == second_role]
        for deposition in (
            part
            for part in parts
            if part.role == "deposition" and part.purpose == purpose
        ):
            left = self._best_contact(deposition, first)
            right = self._best_contact(deposition, second)
            if left.connected and right.connected:
                best = max(best, min(left.section_um, right.section_um))
        return best

    def _best_contact(
        self, deposition: MeshPart, components: list[MeshPart]
    ) -> ContactMetrics:
        best = ContactMetrics(math.inf, 0.0, False)
        for component in components:
            value = contact_metrics(
                deposition.mesh,
                component.mesh,
                epsilon_um=self.epsilon_um,
                min_section_um=self.scenario.tolerances.joint_scale_um,
            )
            if value.section_um > best.section_um:
                best = value
        return best


def _norm(value: tuple[float, ...]) -> float:
    return math.sqrt(sum(item * item for item in value))


def _angle_delta(actual: float, expected: float) -> float:
    return (actual - expected + 180.0) % 360.0 - 180.0


def _canonical_geometry_hash(parts: tuple[MeshPart, ...]) -> str:
    records: list[dict[str, object]] = []
    for part in sorted(parts, key=lambda item: (item.role, item.name, item.purpose or "")):
        triangles = []
        for triangle in part.mesh.triangles:
            vertices = sorted(
                tuple(round(value, 6) for value in vertex) for vertex in triangle
            )
            triangles.append(vertices)
        records.append(
            {
                "name": part.name,
                "role": part.role,
                "purpose": part.purpose,
                "triangles": sorted(triangles),
            }
        )
    payload = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
