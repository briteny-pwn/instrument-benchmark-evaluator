from workflow import run_variant


def run_experiment(microscope, scenario, checkpoint, output_dir):
    return run_variant(microscope, scenario, checkpoint, output_dir, "cut_both_joints")
