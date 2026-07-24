def run_experiment(endpoint, output):
    chunks = []
    while True:
        chunks.append(bytearray(32 * 1024 * 1024))
