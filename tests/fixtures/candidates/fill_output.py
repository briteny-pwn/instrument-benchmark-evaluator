def run_experiment(endpoint, output):
    with open("/output/fill.bin", "wb") as handle:
        while True:
            handle.write(b"x" * 1024 * 1024)
