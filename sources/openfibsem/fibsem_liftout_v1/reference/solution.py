from fibsem_iab import Pattern, Vec3


def _vec(value):
    return Vec3.from_value(value)


def _world_position(scenario, name):
    pose = scenario.frames[name]
    if pose.relative_to == "world":
        return tuple(pose.position_um.to_list())
    parent = _world_position(scenario, pose.relative_to)
    return tuple(
        left + right
        for left, right in zip(parent, pose.position_um.to_list(), strict=True)
    )


def _pattern(purpose, frame, center, size, rotation=0.0):
    return Pattern(
        purpose=purpose,
        frame=frame,
        center_um=_vec(center),
        size_um=_vec(size),
        rotation_degrees=rotation,
    )


def _box(scenario, section, name):
    value = scenario.data[section][name]
    return value["frame"], value["center_um"], value["size_um"]


def _preflight(microscope, scenario):
    length = scenario.characteristic_length_um
    delta = min(1.0, 0.10 * length)
    microscope.ping()
    microscope.capabilities()
    microscope.acquire_image("SEM")
    microscope.acquire_image("FIB")
    microscope.move_stage(_vec((delta, 0.0, 0.0)), relative=True)
    microscope.move_stage(_vec((-delta, 0.0, 0.0)), relative=True)
    microscope.insert_manipulator()
    microscope.move_manipulator(_vec((0.0, delta, 0.0)), relative=True)
    microscope.retract_manipulator()
    microscope.run_cut(
        _pattern("preflight_cut", "coupon", (0.0, 0.0, 2.0), (1.0, 1.0, 1.0))
    )
    microscope.run_deposition(
        _pattern(
            "preflight_deposition",
            "coupon",
            (0.0, 0.0, 2.0),
            (1.0, 1.0, 1.0),
        )
    )


def run_experiment(microscope, scenario, checkpoint, output_dir):
    del output_dir
    _preflight(microscope, scenario)
    sx, sy, sz = scenario.sample_dimensions_um.to_list()
    joint = scenario.joint_scale_um

    microscope.acquire_image("SEM")
    microscope.acquire_image("FIB")
    frame, center, size = _box(scenario, "sample", "protected_region")
    microscope.run_deposition(_pattern("protection", frame, center, size))
    for purpose, y, thickness in (
        ("trench", sy / 2 + 1.0, 2.0),
        ("trench", -sy / 2 - 1.0, 2.0),
        ("polish", sy / 2 + 0.25, 0.5),
        ("polish", -sy / 2 - 0.25, 0.5),
    ):
        microscope.run_cut(
            _pattern(purpose, "sample", (0.0, y, 0.0), (sx, thickness, sz / 2))
        )
    microscope.run_cut(
        _pattern("u_cut", "sample", (sx / 2 + 1.0, 0.0, 0.0), (2.0, sy, sz / 2))
    )
    checkpoint("step_1", {"phase": "sample-prepared"})

    needle_region = scenario.data["needle"]["joint_region"]
    needle_frame = needle_region["frame"]
    needle_center = needle_region["center_um"]
    needle_size = needle_region["size_um"]
    needle_origin = _world_position(scenario, needle_frame)
    needle_tip = (
        needle_origin[0] + needle_center[0] - needle_size[0] / 2,
        needle_origin[1] + needle_center[1],
        needle_origin[2] + needle_center[2],
    )
    microscope.insert_manipulator(_vec(needle_tip))
    microscope.run_deposition(
        _pattern("needle_joint", needle_frame, needle_center, needle_size)
    )
    bridge_frame, bridge_center, bridge_size = _box(
        scenario, "sample", "source_bridge"
    )
    microscope.run_cut(
        _pattern(
            "source_separation",
            bridge_frame,
            bridge_center,
            (
                bridge_size[0] + 1.0,
                bridge_size[1] + 0.5,
                bridge_size[2] + 1.0,
            ),
        )
    )
    carry = max(0.5, 0.05 * scenario.characteristic_length_um)
    microscope.move_manipulator(_vec((0.0, 0.0, carry)), relative=True)
    checkpoint("step_2", {"phase": "source-released"})

    target_origin = _world_position(scenario, "target")
    source_origin = _world_position(scenario, "source")
    microscope.move_stage(
        _vec(
            tuple(
                target - source
                for target, source in zip(target_origin, source_origin, strict=True)
            )
        ),
        relative=True,
    )
    sample_origin = _world_position(scenario, "sample")
    target_pose = _world_position(scenario, "target_pose")
    transfer = (
        target_pose[0] - sample_origin[0],
        target_pose[1] - sample_origin[1],
        target_pose[2] - sample_origin[2] - carry,
    )
    microscope.move_manipulator(_vec(transfer), relative=True)
    target_pose_relative = scenario.frames["target_pose"].position_um.to_list()
    target_joint_center = (
        target_pose_relative[0] - sx / 2,
        target_pose_relative[1] + sy / 2 - joint,
        target_pose_relative[2],
    )
    target_joint_size = (max(2 * joint, 0.4), max(1.5 * joint, 0.3), max(2 * joint, 0.4))
    microscope.run_deposition(
        _pattern("target_joint", "target", target_joint_center, target_joint_size)
    )
    checkpoint("step_3", {"phase": "target-attached"})

    needle_cut_center = (
        target_pose_relative[0] - sx / 2,
        target_pose_relative[1],
        target_pose_relative[2],
    )
    microscope.run_cut(
        _pattern(
            "needle_separation",
            "target",
            needle_cut_center,
            (max(2 * joint, 0.5), max(2 * joint, 0.5), max(2 * joint, 0.5)),
        )
    )
    microscope.retract_manipulator()
    microscope.acquire_image("SEM")
    microscope.acquire_image("FIB")
    checkpoint("step_4", {"phase": "needle-retracted"})
    return {
        "instance_id": "fibsem_liftout_v1",
        "scenario_id": scenario.scenario_id,
        "checkpoints": ["step_1", "step_2", "step_3", "step_4"],
        "completed": True,
    }
