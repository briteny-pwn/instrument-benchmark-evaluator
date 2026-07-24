# Instrument Benchmark Evaluator

Private evaluator implementation for the distributed instrument benchmark.
This repository owns the PyVISA/pyvisa-sim environment, five instrument state
machines, shared DUT world, raw transport gateway, hidden worlds, append-only
evidence, independent oracle, causal constraints, safety gates, scoring,
reference solution, and adversarial cases.

The public process boundary is protocol version 1:

```bash
python -m instrument_benchmark_evaluator.cli run \
  --request /absolute/path/request.json \
  --report /absolute/path/report.json
```

Exit status `0` means evaluation completed, including a candidate that failed
gates. Status `2` means the request/contract is invalid. Status `3` means
evaluator infrastructure failed. Candidate failures are represented inside a
successful evaluator report.

Official candidates run in a fresh locked Docker container for every world.
The evaluator retains the instrument simulator, hidden world, journal, oracle,
scoring, and forced cleanup on the host. The only runtime connection is a
run-scoped Unix socket; containers are non-root, networkless, read-only,
capability-free, resource-limited, and removed after inspect evidence is
captured. Host subprocess execution exists only as an injected test backend.

Run local tests with:

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s evaluators/pyvisa_dut_validation_v1/tests -v
```

Native Linux Docker checks are opt-in locally and mandatory in CI:

```bash
IAB_RUN_DOCKER_TESTS=1 python -m unittest \
  tests.integration.test_container_image_linux \
  tests.integration.test_container_isolation_linux \
  tests.integration.test_docker_full_suite_linux -v
```
