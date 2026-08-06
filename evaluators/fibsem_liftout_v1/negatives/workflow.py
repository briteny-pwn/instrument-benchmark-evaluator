from fibsem_iab import Pattern, Vec3


def _v(value):
    return Vec3.from_value(value)


def _world(scenario, name):
    pose = scenario.frames[name]
    parent = (0.0, 0.0, 0.0) if pose.relative_to == "world" else _world(
        scenario, pose.relative_to
    )
    return tuple(
        left + right
        for left, right in zip(parent, pose.position_um.to_list(), strict=True)
    )


def _p(purpose, frame, center, size):
    return Pattern(purpose, frame, _v(center), _v(size), 0.0)


def _preflight(microscope, scenario):
    delta = min(1.0, 0.1 * scenario.characteristic_length_um)
    microscope.ping()
    microscope.capabilities()
    microscope.acquire_image("SEM")
    microscope.acquire_image("FIB")
    microscope.move_stage(_v((delta, 0, 0)), relative=True)
    microscope.move_stage(_v((-delta, 0, 0)), relative=True)
    microscope.insert_manipulator()
    microscope.move_manipulator(_v((0, delta, 0)), relative=True)
    microscope.retract_manipulator()
    microscope.run_cut(_p("preflight_cut", "coupon", (0, 0, 2), (1, 1, 1)))
    microscope.run_deposition(
        _p("preflight_deposition", "coupon", (0, 0, 2), (1, 1, 1))
    )


def run_variant(microscope, scenario, checkpoint, output_dir, behavior):
    if behavior == "fake_checkpoint":
        output_dir.mkdir(parents=True, exist_ok=True)
        for step in ("step_1", "step_2", "step_3", "step_4"):
            (output_dir / f"{step}.json").write_text('{"trusted":true}')
        return {
            "instance_id": "fibsem_liftout_v1",
            "scenario_id": scenario.scenario_id,
            "checkpoints": ["step_1", "step_2", "step_3", "step_4"],
            "completed": True,
        }

    _preflight(microscope, scenario)
    if behavior == "hardcoded_nominal":
        sx, sy, sz, joint = 14.0, 8.0, 10.0, 0.3115
    else:
        sx, sy, sz = scenario.sample_dimensions_um.to_list()
        joint = scenario.joint_scale_um
    protection = scenario.data["sample"]["protected_region"]
    microscope.run_deposition(
        _p("protection", protection["frame"], protection["center_um"], protection["size_um"])
    )
    microscope.run_cut(
        _p("u_cut", "sample", (sx / 2 + 1, 0, 0), (2, sy, sz / 2))
    )
    bridge = scenario.data["sample"]["source_bridge"]
    if behavior == "cut_source_early":
        microscope.run_cut(
            _p(
                "source_separation",
                bridge["frame"],
                bridge["center_um"],
                (bridge["size_um"][0] + 1, bridge["size_um"][1] + 0.5, bridge["size_um"][2] + 1),
            )
        )
    checkpoint("step_1")

    region = scenario.data["needle"]["joint_region"]
    if behavior == "hardcoded_nominal":
        region_center, region_size = (-7.0, 0.0, 0.0), (4.0, 4.0, 4.0)
    else:
        region_center, region_size = region["center_um"], region["size_um"]
    origin = _world(scenario, region["frame"])
    tip = (
        origin[0] + region_center[0] - region_size[0] / 2,
        origin[1] + region_center[1],
        origin[2] + region_center[2],
    )
    microscope.insert_manipulator(_v(tip))
    microscope.run_deposition(
        _p("needle_joint", region["frame"], region_center, region_size)
    )
    if behavior not in {"cut_source_early", "no_source_cut"}:
        microscope.run_cut(
            _p(
                "source_separation",
                bridge["frame"],
                bridge["center_um"],
                (bridge["size_um"][0] + 1, bridge["size_um"][1] + 0.5, bridge["size_um"][2] + 1),
            )
        )
    carry = max(0.5, 0.05 * scenario.characteristic_length_um)
    microscope.move_manipulator(_v((0, 0, carry)), relative=True)
    if behavior == "cut_needle_early":
        microscope.run_cut(
            _p("needle_separation", "sample", region_center, (1, 1, 1))
        )
    checkpoint("step_2")

    target_origin, source_origin = _world(scenario, "target"), _world(scenario, "source")
    microscope.move_stage(
        _v(tuple(a - b for a, b in zip(target_origin, source_origin, strict=True))),
        relative=True,
    )
    if behavior == "hardcoded_nominal":
        target_pose = (-989.0, 0.0, 6.0)
        target_relative = (-9.0, 0.0, 6.0)
    else:
        target_pose = _world(scenario, "target_pose")
        target_relative = tuple(scenario.frames["target_pose"].position_um.to_list())
    sample_origin = _world(scenario, "sample")
    microscope.move_manipulator(
        _v(
            (
                target_pose[0] - sample_origin[0],
                target_pose[1] - sample_origin[1],
                target_pose[2] - sample_origin[2] - carry,
            )
        ),
        relative=True,
    )
    target_center = (
        target_relative[0] - sx / 2,
        target_relative[1] + sy / 2 - joint,
        target_relative[2],
    )
    target_size = (max(2 * joint, 0.4), max(1.5 * joint, 0.3), max(2 * joint, 0.4))
    if behavior != "no_target_deposition":
        microscope.run_deposition(_p("target_joint", "target", target_center, target_size))
    checkpoint("step_3")

    cut_center = (
        target_relative[0] - sx / 2,
        target_relative[1],
        target_relative[2],
    )
    cut_size = (
        max(2 * joint, 0.5),
        sy if behavior == "cut_both_joints" else max(2 * joint, 0.5),
        max(2 * joint, 0.5),
    )
    if behavior != "cut_needle_early":
        microscope.run_cut(_p("needle_separation", "target", cut_center, cut_size))
    if behavior != "no_retract":
        microscope.retract_manipulator()
    checkpoint("step_4")
    return {
        "instance_id": "fibsem_liftout_v1",
        "scenario_id": scenario.scenario_id,
        "checkpoints": ["step_1", "step_2", "step_3", "step_4"],
        "completed": True,
    }
