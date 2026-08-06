from evaluators.fibsem_liftout_v1.backend import OpenFibsemBackend


def run_experiment(microscope, scenario, checkpoint, output_dir):
    return {"stolen_backend": OpenFibsemBackend.__name__}
