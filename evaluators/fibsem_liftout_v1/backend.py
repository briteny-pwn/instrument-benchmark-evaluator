from __future__ import annotations

import base64
import math
import os
import threading
from pathlib import Path
from typing import Mapping, Protocol

from .geometry.metrics import (
    Bounds,
    MaterialChange,
    MeshPart,
    PoseState,
    SceneSnapshot,
    TriangleMesh,
    box_mesh,
)
from .geometry.oracle import _canonical_geometry_hash
from .models import ScenarioSpec, vec3


OPENFIBSEM_COMMIT = "2ebccb8b9721234ca66bb94de36d0f7cfe047af9"


class Runtime(Protocol):
    source_commit: str

    def ping(self) -> bool: ...

    def acquire_image(self, beam: str) -> tuple[int, int, bytes]: ...

    def move_stage(self, position: tuple[float, float, float]) -> None: ...

    def move_manipulator(
        self, position: tuple[float, float, float], *, inserted: bool
    ) -> None: ...

    def run_pattern(
        self, operation: str, purpose: str, pattern: dict[str, object]
    ) -> None: ...

    def stop(self, kind: str) -> None: ...

    def synchronize(self, parts: tuple[MeshPart, ...]) -> None: ...

    def close(self) -> None: ...


class OpenFibsemBackend:
    """Trusted semantic adapter around the pinned OpenFIBSEM simulator runtime."""

    def __init__(self, scenario: ScenarioSpec, *, runtime: Runtime | None = None):
        self.scenario = scenario
        self._lock = threading.RLock()
        self._stage = (0.0, 0.0, 0.0)
        self._needle = scenario.world_position("needle")
        self._needle_inserted = False
        self._active_operation: str | None = None
        self._collision = False
        self._changes: list[MaterialChange] = []
        self._operation_number = 0
        self._parts = list(self._initial_parts())
        self.runtime: Runtime = runtime or _OpenFibsemRuntime(scenario, tuple(self._parts))
        if self.runtime.source_commit != OPENFIBSEM_COMMIT:
            raise RuntimeError("OpenFIBSEM source commit mismatch")
        self.runtime.synchronize(tuple(self._parts))

    def semantic_state(self) -> dict[str, object]:
        with self._lock:
            return {
                "stage": list(self._stage),
                "needle": list(self._needle),
                "inserted": self._needle_inserted,
                "active_operation": self._active_operation,
                "collision": self._collision,
                "geometry_hash": _canonical_geometry_hash(tuple(self._parts)),
                "material_change_count": len(self._changes),
            }

    def invoke(self, operation: str, arguments: dict[str, object]) -> object:
        with self._lock:
            handlers = {
                "ping": self._ping,
                "capabilities": self._capabilities,
                "acquire_image": self._acquire_image,
                "get_stage_position": self._get_stage_position,
                "move_stage": self._move_stage,
                "stop_stage": self._stop_stage,
                "get_manipulator_state": self._get_manipulator_state,
                "insert_manipulator": self._insert_manipulator,
                "move_manipulator": self._move_manipulator,
                "retract_manipulator": self._retract_manipulator,
                "stop_manipulator": self._stop_manipulator,
                "run_cut": self._run_cut,
                "run_deposition": self._run_deposition,
                "pattern_status": self._pattern_status,
                "stop_pattern": self._stop_pattern,
            }
            if operation not in handlers:
                raise ValueError(f"unsupported trusted backend operation: {operation}")
            return handlers[operation](arguments)

    def motion_is_safe(
        self, kind: str, target_um: tuple[float, float, float]
    ) -> bool:
        if kind not in {"stage", "manipulator"}:
            return False
        return all(math.isfinite(value) and abs(value) <= 5000.0 for value in target_um)

    def freeze_snapshot(self, step_id: str) -> SceneSnapshot:
        with self._lock:
            parts = tuple(
                MeshPart(
                    part.name,
                    part.role,
                    TriangleMesh(tuple(part.mesh.vertices), tuple(part.mesh.faces)),
                    part.purpose,
                )
                for part in self._parts
            )
            sample_parts = [part for part in parts if part.role == "sample"]
            sample_center = (
                _weighted_center(sample_parts)
                if sample_parts
                else self.scenario.world_position("sample")
            )
            return SceneSnapshot(
                checkpoint_id=step_id,
                parts=parts,
                poses={
                    "sample": PoseState(
                        sample_center,
                        self.scenario.frames["target_pose"].orientation_degrees
                        if self._has_purpose("target_joint")
                        else self.scenario.frames["sample"].orientation_degrees,
                    ),
                    "needle": PoseState(self._needle, (0.0, 0.0, 0.0)),
                },
                planned_sample_volume_um3=math.prod(
                    self.scenario.sample_dimensions_um
                ),
                material_changes=tuple(self._changes),
                needle_inserted=self._needle_inserted,
                active_operation=self._active_operation is not None,
                collision=self._collision,
            )

    def acquire_checkpoint_images(self) -> dict[str, tuple[int, int, bytes]]:
        with self._lock:
            return {
                "SEM": self.runtime.acquire_image("SEM"),
                "FIB": self.runtime.acquire_image("FIB"),
            }

    def cancel(self) -> None:
        with self._lock:
            for kind in ("pattern", "stage", "manipulator"):
                self.runtime.stop(kind)
            self._active_operation = None

    def force_safe(self) -> None:
        with self._lock:
            if self._needle_inserted:
                if self._has_purpose("target_joint"):
                    self._parts = [
                        part
                        for part in self._parts
                        if not (
                            part.role == "deposition"
                            and part.purpose == "needle_joint"
                        )
                    ]
                self._move_needle_geometry(self.scenario.world_position("needle"))
                self._needle_inserted = False
                self.runtime.move_manipulator(self._needle, inserted=False)
                self.runtime.synchronize(tuple(self._parts))
            self._active_operation = None

    def close(self) -> None:
        self.runtime.close()

    def _ping(self, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "connected": bool(self.runtime.ping()),
            "simulator": "OpenFIBSEM",
            "source_commit": OPENFIBSEM_COMMIT,
        }

    def _capabilities(self, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "beams": ["SEM", "FIB"],
            "stage": True,
            "manipulator": True,
            "cut": True,
            "deposition": True,
            "checkpoints": 4,
        }

    def _acquire_image(self, arguments: dict[str, object]) -> dict[str, object]:
        beam = arguments["beam"]
        assert isinstance(beam, str)
        width, height, pixels = self.runtime.acquire_image(beam)
        if len(pixels) != width * height:
            raise RuntimeError("OpenFIBSEM image dimensions are inconsistent")
        return {
            "beam": beam,
            "width": width,
            "height": height,
            "pixels_base64": base64.b64encode(pixels).decode("ascii"),
            "metadata": {
                "simulator": "OpenFIBSEM",
                "source_commit": OPENFIBSEM_COMMIT,
            },
        }

    def _get_stage_position(self, arguments: dict[str, object]) -> dict[str, object]:
        return _pose(self._stage)

    def _move_stage(self, arguments: dict[str, object]) -> dict[str, object]:
        self._stage = _target(self._stage, arguments)
        self.runtime.move_stage(self._stage)
        return _pose(self._stage)

    def _stop_stage(self, arguments: dict[str, object]) -> None:
        self.runtime.stop("stage")
        return None

    def _get_manipulator_state(
        self, arguments: dict[str, object]
    ) -> dict[str, object]:
        return {"inserted": self._needle_inserted, "pose": _pose(self._needle)}

    def _insert_manipulator(self, arguments: dict[str, object]) -> dict[str, object]:
        position = (
            vec3(arguments["position_um"], "manipulator position")
            if "position_um" in arguments
            else self._needle
        )
        self._needle_inserted = True
        self._move_needle_geometry(position)
        self.runtime.move_manipulator(position, inserted=True)
        self.runtime.synchronize(tuple(self._parts))
        return _pose(self._needle)

    def _move_manipulator(self, arguments: dict[str, object]) -> dict[str, object]:
        position = _target(self._needle, arguments)
        self._move_needle_geometry(position)
        self.runtime.move_manipulator(position, inserted=self._needle_inserted)
        self.runtime.synchronize(tuple(self._parts))
        return _pose(self._needle)

    def _retract_manipulator(self, arguments: dict[str, object]) -> dict[str, object]:
        position = self.scenario.world_position("needle")
        self._move_needle_geometry(position)
        self._needle_inserted = False
        self.runtime.move_manipulator(position, inserted=False)
        self.runtime.synchronize(tuple(self._parts))
        return _pose(self._needle)

    def _stop_manipulator(self, arguments: dict[str, object]) -> None:
        self.runtime.stop("manipulator")
        return None

    def _run_cut(self, arguments: dict[str, object]) -> dict[str, object]:
        pattern = arguments["pattern"]
        assert isinstance(pattern, dict)
        purpose = pattern["purpose"]
        assert isinstance(purpose, str)
        self._begin_pattern("cut", purpose, pattern)
        bounds = self._pattern_bounds(pattern)
        self._changes.append(
            MaterialChange("cut", purpose, bounds, -_bounds_volume(bounds))
        )
        if purpose == "source_separation":
            self._parts = [
                part
                for part in self._parts
                if not (part.name == "source_bridge" and part.mesh.bounds.overlaps(bounds))
            ]
        elif purpose == "needle_separation":
            self._parts = [
                part
                for part in self._parts
                if not (
                    part.role == "deposition"
                    and part.mesh.bounds.overlaps(bounds)
                    and part.purpose in {"needle_joint", "target_joint"}
                )
            ]
        elif purpose in {"trench", "polish", "u_cut"}:
            self._cut_sample(bounds)
        self._finish_pattern()
        return self._receipt()

    def _run_deposition(self, arguments: dict[str, object]) -> dict[str, object]:
        pattern = arguments["pattern"]
        assert isinstance(pattern, dict)
        purpose = pattern["purpose"]
        assert isinstance(purpose, str)
        self._begin_pattern("deposition", purpose, pattern)
        bounds = self._pattern_bounds(pattern)
        self._changes.append(
            MaterialChange("deposition", purpose, bounds, _bounds_volume(bounds))
        )
        self._parts.append(
            MeshPart(
                f"{purpose}_{self._operation_number}",
                "deposition",
                box_mesh(center=_bounds_center(bounds), size=bounds.size),
                purpose=purpose,
            )
        )
        self._finish_pattern()
        return self._receipt()

    def _pattern_status(self, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "operation_id": arguments["operation_id"],
            "status": "running" if self._active_operation else "completed",
        }

    def _stop_pattern(self, arguments: dict[str, object]) -> dict[str, object]:
        self.runtime.stop("pattern")
        self._active_operation = None
        return {"operation_id": arguments["operation_id"], "status": "stopped"}

    def _begin_pattern(
        self, operation: str, purpose: str, pattern: dict[str, object]
    ) -> None:
        self._operation_number += 1
        self._active_operation = f"{operation}:{purpose}"
        runtime_pattern = dict(pattern)
        frame = pattern["frame"]
        assert isinstance(frame, str)
        center = vec3(pattern["center_um"], "pattern center")
        origin = self.scenario.world_position(frame)
        runtime_pattern["frame"] = "world"
        runtime_pattern["center_um"] = [
            base + offset for base, offset in zip(origin, center, strict=True)
        ]
        self.runtime.run_pattern(operation, purpose, runtime_pattern)

    def _finish_pattern(self) -> None:
        self.runtime.synchronize(tuple(self._parts))
        self._active_operation = None

    def _receipt(self) -> dict[str, object]:
        return {
            "operation_id": f"pattern-{self._operation_number:08d}",
            "status": "completed",
        }

    def _move_needle_geometry(self, position: tuple[float, float, float]) -> None:
        delta = tuple(
            target - current
            for target, current in zip(position, self._needle, strict=True)
        )
        carry_sample = not any(part.name == "source_bridge" for part in self._parts) and self._has_purpose(
            "needle_joint"
        )
        attached_to_target = self._has_purpose("target_joint")
        if carry_sample and attached_to_target and any(abs(value) > 1e-9 for value in delta):
            self._collision = True
        moved: list[MeshPart] = []
        for part in self._parts:
            should_move = part.role == "needle" or (
                carry_sample
                and (
                    part.role == "sample"
                    or (part.role == "deposition" and part.purpose == "needle_joint")
                )
            )
            moved.append(_translated_part(part, delta) if should_move else part)
        self._parts = moved
        self._needle = position

    def _has_purpose(self, purpose: str) -> bool:
        return any(
            part.role == "deposition" and part.purpose == purpose
            for part in self._parts
        )

    def _pattern_bounds(self, pattern: Mapping[str, object]) -> Bounds:
        frame = pattern["frame"]
        assert isinstance(frame, str)
        center = vec3(pattern["center_um"], "pattern center")
        size = vec3(pattern["size_um"], "pattern size")
        rotation = math.radians(float(pattern["rotation_degrees"]))
        extent = (
            abs(math.cos(rotation)) * size[0] + abs(math.sin(rotation)) * size[1],
            abs(math.sin(rotation)) * size[0] + abs(math.cos(rotation)) * size[1],
            size[2],
        )
        origin = self.scenario.world_position(frame)
        world = tuple(base + offset for base, offset in zip(origin, center, strict=True))
        return Bounds(
            tuple(value - width / 2 for value, width in zip(world, extent, strict=True)),  # type: ignore[arg-type]
            tuple(value + width / 2 for value, width in zip(world, extent, strict=True)),  # type: ignore[arg-type]
        )

    def _cut_sample(self, cutter: Bounds) -> None:
        output: list[MeshPart] = []
        for part in self._parts:
            if part.role != "sample" or not part.mesh.bounds.overlaps(cutter):
                output.append(part)
                continue
            fragments = _subtract_bounds(part.mesh.bounds, cutter)
            output.extend(
                MeshPart(
                    f"{part.name}_cut_{index}",
                    "sample",
                    box_mesh(center=_bounds_center(bounds), size=bounds.size),
                )
                for index, bounds in enumerate(fragments, start=1)
            )
        self._parts = output

    def _initial_parts(self) -> tuple[MeshPart, ...]:
        sample_center = self.scenario.world_position("sample")
        sx, sy, sz = self.scenario.sample_dimensions_um
        bridge_data = self.scenario.data["sample"]
        assert isinstance(bridge_data, Mapping)
        bridge = bridge_data["source_bridge"]
        assert isinstance(bridge, Mapping)
        bridge_frame = bridge["frame"]
        assert isinstance(bridge_frame, str)
        bridge_center = tuple(
            base + offset
            for base, offset in zip(
                self.scenario.world_position(bridge_frame),
                vec3(bridge["center_um"], "source bridge center"),
                strict=True,
            )
        )
        bridge_size = list(vec3(bridge["size_um"], "source bridge size"))
        bridge_size[2] += 0.5
        bridge_center = (bridge_center[0], bridge_center[1], bridge_center[2] - 0.25)
        source_top = sample_center[2] - sz / 2 - 0.25
        source_size_z = 10.0
        needle_tip = self.scenario.world_position("needle")
        needle_center = (needle_tip[0] - 3.0, needle_tip[1], needle_tip[2])
        target_pose = self.scenario.world_position("target_pose")
        target_center = (target_pose[0] - sx / 2 - 3.0, target_pose[1], target_pose[2])
        coupon = self.scenario.world_position("coupon")
        return (
            MeshPart(
                "source_base",
                "source",
                box_mesh(
                    center=(sample_center[0], sample_center[1], source_top - source_size_z / 2),
                    size=(max(40.0, sx * 2), max(30.0, sy * 2), source_size_z),
                ),
            ),
            MeshPart(
                "source_bridge",
                "source",
                box_mesh(center=bridge_center, size=bridge_size),
            ),
            MeshPart(
                "sample",
                "sample",
                box_mesh(center=sample_center, size=self.scenario.sample_dimensions_um),
            ),
            MeshPart(
                "needle",
                "needle",
                box_mesh(center=needle_center, size=(6.0, 2.0, 2.0)),
            ),
            MeshPart(
                "target",
                "target",
                box_mesh(center=target_center, size=(6.0, max(20.0, sy * 2), max(12.0, sz))),
            ),
            MeshPart(
                "coupon",
                "coupon",
                box_mesh(center=(coupon[0], coupon[1], coupon[2] - 1.0), size=(14.0, 14.0, 2.0)),
            ),
        )


def _pose(position: tuple[float, float, float]) -> dict[str, object]:
    return {
        "relative_to": "world",
        "position_um": list(position),
        "orientation_degrees": [0.0, 0.0, 0.0],
    }


def _target(
    current: tuple[float, float, float], arguments: Mapping[str, object]
) -> tuple[float, float, float]:
    requested = vec3(arguments["position_um"], "motion position")
    if arguments["relative"] is True:
        return tuple(
            left + right for left, right in zip(current, requested, strict=True)
        )  # type: ignore[return-value]
    return requested


def _translated_part(
    part: MeshPart, delta: tuple[float, float, float]
) -> MeshPart:
    vertices = tuple(
        tuple(value + movement for value, movement in zip(vertex, delta, strict=True))
        for vertex in part.mesh.vertices
    )
    return MeshPart(
        part.name,
        part.role,
        TriangleMesh(vertices, part.mesh.faces),  # type: ignore[arg-type]
        part.purpose,
    )


def _weighted_center(parts: list[MeshPart]) -> tuple[float, float, float]:
    volume = sum(part.mesh.volume_um3 for part in parts)
    if volume <= 0:
        return parts[0].mesh.centroid
    return tuple(
        sum(part.mesh.centroid[index] * part.mesh.volume_um3 for part in parts) / volume
        for index in range(3)
    )  # type: ignore[return-value]


def _bounds_center(bounds: Bounds) -> tuple[float, float, float]:
    return tuple(
        (low + high) / 2
        for low, high in zip(bounds.minimum, bounds.maximum, strict=True)
    )  # type: ignore[return-value]


def _bounds_volume(bounds: Bounds) -> float:
    return math.prod(bounds.size)


def _subtract_bounds(source: Bounds, cutter: Bounds) -> tuple[Bounds, ...]:
    intersection = Bounds(
        tuple(max(a, b) for a, b in zip(source.minimum, cutter.minimum, strict=True)),  # type: ignore[arg-type]
        tuple(min(a, b) for a, b in zip(source.maximum, cutter.maximum, strict=True)),  # type: ignore[arg-type]
    )
    if any(size <= 1e-9 for size in intersection.size):
        return (source,)
    x0, y0, z0 = source.minimum
    x1, y1, z1 = source.maximum
    ix0, iy0, iz0 = intersection.minimum
    ix1, iy1, iz1 = intersection.maximum
    candidates = (
        Bounds((x0, y0, z0), (ix0, y1, z1)),
        Bounds((ix1, y0, z0), (x1, y1, z1)),
        Bounds((ix0, y0, z0), (ix1, iy0, z1)),
        Bounds((ix0, iy1, z0), (ix1, y1, z1)),
        Bounds((ix0, iy0, z0), (ix1, iy1, iz0)),
        Bounds((ix0, iy0, iz1), (ix1, iy1, z1)),
    )
    return tuple(bounds for bounds in candidates if min(bounds.size) > 1e-9)


def openfibsem_source_commit() -> str:
    import fibsem  # type: ignore[import-not-found]  # noqa: F401

    return OPENFIBSEM_COMMIT


class _OpenFibsemRuntime:
    source_commit = OPENFIBSEM_COMMIT
    use_ray_tracing = True
    ray_tracing_cpu_threads = 0

    def __init__(self, scenario: ScenarioSpec, parts: tuple[MeshPart, ...]):
        from fibsem import utils  # type: ignore[import-not-found]
        from fibsem.structures import (  # type: ignore[import-not-found]
            FibsemManipulatorPosition,
            FibsemStagePosition,
        )

        runtime_root = Path(
            os.environ.get("FIBSEM_IAB_RUNTIME_DIR", "/tmp/fibsem-iab-runtime")
        )
        runtime_root.mkdir(parents=True, exist_ok=True)
        sample_mesh, needle_mesh = _parts_to_pyvista(parts)
        needle = scenario.world_position("needle")
        self.microscope, _settings = utils.setup_session(
            session_path=runtime_root,
            setup_logging=False,
            manufacturer="Simulator",
            sample_mesh=sample_mesh,
            copper_centers=[],
            needle_mesh=needle_mesh,
            max_size="50 um",
            max_defocus="0 um",
            max_depo_depth="20 um",
            clear_after_cut=False,
            check_collision=False,
            use_ssao=False,
            use_rt=self.use_ray_tracing,
            rt_cpu_threads=self.ray_tracing_cpu_threads,
            patterning_sleep=-1.0,
            manipulator_retract_pos=FibsemManipulatorPosition(
                x=f"{needle[0]} um", y=f"{needle[1]} um", z=f"{needle[2]} um"
            ),
            stage_init=FibsemStagePosition(x="0 um", y="0 um", z="0 um"),
            del_display=True,
        )

    def ping(self) -> bool:
        return bool(self.microscope.ping())

    def acquire_image(self, beam: str) -> tuple[int, int, bytes]:
        import numpy as np  # type: ignore[import-not-found]
        from fibsem.structures import BeamType, ImageSettings  # type: ignore[import-not-found]

        beam_type = BeamType.ELECTRON if beam == "SEM" else BeamType.ION
        image = self.microscope.acquire_image(
            ImageSettings(resolution=[512, 512], hfw="80 um", beam_type=beam_type)
        )
        data = np.asarray(image.data)
        if data.ndim == 3:
            data = data[..., :3].mean(axis=2)
        if data.dtype != np.uint8:
            minimum, maximum = float(data.min()), float(data.max())
            data = (
                np.zeros(data.shape, dtype=np.uint8)
                if maximum <= minimum
                else ((data - minimum) * 255.0 / (maximum - minimum)).astype(np.uint8)
            )
        height, width = data.shape
        return int(width), int(height), data.tobytes(order="C")

    def move_stage(self, position: tuple[float, float, float]) -> None:
        from fibsem.structures import FibsemStagePosition  # type: ignore[import-not-found]

        self.microscope.move_stage_absolute(
            FibsemStagePosition(
                x=f"{position[0]} um", y=f"{position[1]} um", z=f"{position[2]} um"
            )
        )

    def move_manipulator(
        self, position: tuple[float, float, float], *, inserted: bool
    ) -> None:
        from fibsem.structures import FibsemManipulatorPosition  # type: ignore[import-not-found]

        self.microscope.move_manipulator_absolute(
            FibsemManipulatorPosition(
                x=f"{position[0]} um", y=f"{position[1]} um", z=f"{position[2]} um"
            )
        )

    def run_pattern(
        self, operation: str, purpose: str, pattern: dict[str, object]
    ) -> None:
        from fibsem.structures.enhanced.fib_setting import (  # type: ignore[import-not-found]
            IDepositionSettings,
            IEtchingSettings,
            PatterningSettings,
        )
        from fibsem.structures.enhanced.geometry_setting import (  # type: ignore[import-not-found]
            RectangleSettings,
        )

        deposition = operation == "deposition"
        settings = PatterningSettings(
            settings=IDepositionSettings() if deposition else IEtchingSettings(),
            label="Ion_deposition" if deposition else "ion_cut",
        )
        layer = self.microscope.setup_patterning(settings)
        center = vec3(pattern["center_um"], "pattern center")
        size = vec3(pattern["size_um"], "pattern size")
        rectangle = RectangleSettings(
            rectype="Filled",
            CenterX=f"{center[0]} um",
            CenterY=f"{center[1]} um",
            Depth=f"{size[2]} um",
            Width=f"{size[0]} um",
            Height=f"{size[1]} um",
            Angle=f"{float(pattern['rotation_degrees'])} deg",
        )
        rectangle.layer = layer
        layer = self.microscope.add_Rectangle(rectangle)
        self.microscope.start_patterning(layer, settings.label)

    def stop(self, kind: str) -> None:
        {
            "pattern": self.microscope.stop_patterning,
            "stage": self.microscope.stop_stage,
            "manipulator": self.microscope.stop_manipulator,
        }[kind]()

    def synchronize(self, parts: tuple[MeshPart, ...]) -> None:
        import numpy as np  # type: ignore[import-not-found]

        stage, needle = _parts_to_pyvista(parts)
        simulator = self.microscope.simulator
        fib = simulator.simulator
        stage_fib = simulator.mesh_sem2fib(stage)
        needle_fib = simulator.mesh_sem2fib(needle)
        meshes = {
            "iab_scene": stage_fib,
            fib._NEEDLE: needle_fib,
        }
        coords = {
            "iab_scene": np.asarray(stage_fib.center, dtype=np.float32),
            fib._NEEDLE: np.asarray(needle_fib.center, dtype=np.float32),
        }
        fib._update(meshes, coords, {}, {})

    def close(self) -> None:
        self.microscope.disconnect()


def _parts_to_pyvista(parts: tuple[MeshPart, ...]):
    import numpy as np  # type: ignore[import-not-found]
    import pyvista as pv  # type: ignore[import-not-found]

    stage_meshes = []
    needle_meshes = []
    for part in parts:
        faces = np.asarray(
            [[3, face[0], face[1], face[2]] for face in part.mesh.faces],
            dtype=np.int64,
        ).reshape(-1)
        mesh = pv.PolyData(np.asarray(part.mesh.vertices), faces)
        (needle_meshes if part.role == "needle" else stage_meshes).append(mesh)
    stage = pv.PolyData().append_polydata(*stage_meshes, inplace=False)
    needle = pv.PolyData().append_polydata(*needle_meshes, inplace=False)
    return stage, needle
