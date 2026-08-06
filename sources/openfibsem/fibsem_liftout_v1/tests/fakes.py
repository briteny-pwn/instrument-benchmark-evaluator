from __future__ import annotations

from sources.openfibsem.fibsem_liftout_v1.geometry.metrics import (
    MeshPart,
    PoseState,
    SceneSnapshot,
    box_mesh,
)


class RecordingRuntime:
    source_commit = "2ebccb8b9721234ca66bb94de36d0f7cfe047af9"

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.stage = (0.0, 0.0, 0.0)
        self.needle = (-28.0, 0.0, 7.0)
        self.inserted = False
        self.safe = True

    def ping(self) -> bool:
        self.calls.append("ping")
        return True

    def acquire_image(self, beam: str) -> tuple[int, int, bytes]:
        self.calls.append(("acquire_image", beam))
        return 2, 2, bytes((0, 85, 170, 255))

    def move_stage(self, position: tuple[float, float, float]) -> None:
        self.calls.append(("move_stage", position))
        self.stage = position

    def move_manipulator(
        self, position: tuple[float, float, float], *, inserted: bool
    ) -> None:
        self.calls.append(("move_manipulator", position, inserted))
        self.needle = position
        self.inserted = inserted

    def run_pattern(
        self, operation: str, purpose: str, pattern: dict[str, object]
    ) -> None:
        self.calls.append(("run_pattern", operation, purpose, pattern))

    def stop(self, kind: str) -> None:
        self.calls.append(("stop", kind))

    def synchronize(self, parts: tuple[MeshPart, ...]) -> None:
        self.calls.append(("synchronize", tuple(part.name for part in parts)))

    def close(self) -> None:
        self.calls.append("close")


def valid_snapshot(step_id: str = "step_1") -> SceneSnapshot:
    return SceneSnapshot(
        checkpoint_id=step_id,
        parts=(
            MeshPart("source", "source", box_mesh(center=(0, 0, -3), size=(30, 30, 5))),
            MeshPart("sample", "sample", box_mesh(center=(0, 0, 5), size=(14, 8, 10))),
            MeshPart("needle", "needle", box_mesh(center=(-10, 0, 5), size=(4, 2, 2))),
            MeshPart("target", "target", box_mesh(center=(-996, 0, 6), size=(6, 20, 12))),
            MeshPart(
                "protection",
                "deposition",
                box_mesh(center=(0, 0, 10.25), size=(4, 2, 0.5)),
                purpose="protection",
            ),
        ),
        poses={
            "sample": PoseState((0, 0, 5), (0, 0, 0)),
            "needle": PoseState((-10, 0, 5), (0, 0, 0)),
        },
        planned_sample_volume_um3=1120.0,
        material_changes=(),
        needle_inserted=True,
        active_operation=False,
        collision=False,
    )


class RecordingBackend:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on
        self.safe = False

    def semantic_state(self) -> dict[str, object]:
        return {
            "stage": [0.0, 0.0, 0.0],
            "needle": [-10.0, 0.0, 5.0],
            "inserted": not self.safe,
            "safe": self.safe,
        }

    def invoke(self, operation: str, arguments: dict[str, object]) -> object:
        self.calls.append(operation)
        if operation == self.fail_on:
            raise RuntimeError("injected backend failure")
        return {"ok": True}

    def motion_is_safe(self, kind: str, target_um: tuple[float, float, float]) -> bool:
        return True

    def freeze_snapshot(self, step_id: str) -> SceneSnapshot:
        self.calls.append("freeze_snapshot")
        return valid_snapshot(step_id)

    def acquire_checkpoint_images(self) -> dict[str, tuple[int, int, bytes]]:
        self.calls.append("acquire_checkpoint_images")
        image = (2, 2, bytes((0, 85, 170, 255)))
        return {"SEM": image, "FIB": image}

    def cancel(self) -> None:
        self.calls.append("cancel")

    def force_safe(self) -> None:
        self.calls.append("force_safe")
        self.safe = True

    def close(self) -> None:
        self.calls.append("close")
