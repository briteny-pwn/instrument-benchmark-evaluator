import os
import time


def run_experiment(endpoint, output):
    children = []
    while len(children) < 256:
        child = os.fork()
        if child == 0:
            time.sleep(10)
            os._exit(0)
        children.append(child)
    return {}
