# Instrument Benchmark Evaluator

Private evaluator implementation for the distributed instrument benchmark.
This repository owns the PyVISA/pyvisa-sim environment, five instrument state
machines, shared DUT world, raw transport gateway, hidden worlds, append-only
evidence, independent oracle, causal constraints, safety gates, scoring,
reference solution, and adversarial cases. Evaluators are addressed by
`(source_id, evaluator_id)` and live only at
`sources/<source_id>/<evaluator_id>/`; each source's `source.yaml` is the
authoritative registry. The pre-migration ungrouped package and manifest paths
are invalid, with no compatibility fallback, alias, or search.

The evaluator request boundary is protocol version 2 and carries
`source_id`, `instance_id`, and `evaluator_id`. Candidate container protocol
version 1 remains unchanged:

```bash
python -m instrument_benchmark_evaluator.cli run \
  --request /absolute/path/request.json \
  --report /absolute/path/report.json
```

Exit status `0` means evaluation completed, including a candidate that failed
gates. Status `2` means the request/contract is invalid. Status `3` means
evaluator infrastructure failed. Candidate failures are represented inside a
successful evaluator report.

Official evaluation installs this repository only in a trusted, offline-built
outer container. The outer container retains the instrument simulator, hidden
world, journal, oracle, scoring, and forced cleanup; it is non-root,
networkless, read-only, capability-free and resource-limited. Its one powerful
resource is the host Docker socket, used to create a fresh locked sibling
candidate container for every world. Possession of that socket is effectively
daemon authority, so only the trusted evaluator may receive it.

This repository also owns the evaluator container build assets under
`container/`: pinned Dockerfiles, Python wheelhouses, FIBSEM system packages,
the Linux/amd64 Docker CLI and Buildx plugin, and their SHA-256 manifests.
Instrument orchestration consumes this directory through
`EVALUATOR_REPO_PATH`; the instrument repository does not carry a second copy.

Candidate siblings never receive the Docker socket, evaluator package, hidden
worlds, simulator YAML, oracle, journal, outer request/report, or Git metadata.
Their only runtime connection is a run-scoped Unix gateway socket, plus their
workspace and bounded output mount. They are non-root, networkless, read-only,
capability-free, resource-limited, inspected, and removed after every world.
The bootstrap files are staged below the canonical shared run root so all bind
sources are visible to the host daemon. Host evaluator execution exists only as
an injected unit-test fixture; official orchestration has no host-backend mode.

The formal v2 path splits each world into two workload siblings. The candidate
uses ordinary `pyvisa.ResourceManager("@iab")`; the public `pyvisa_iab` backend
forwards only PyVISA's existing low-level operations over a run-scoped Unix
socket. A separate UID `11001:11001` sim sibling owns the hidden PyVISA-sim
definition, DUT state, broker, and complete hash-chained event journal. Both
siblings use `network=none`; the candidate sees only the socket directory
read-only, while the sim sees no candidate workspace. The trusted outer
evaluator is the sole holder of Docker authority and removes the candidate
before finalizing and removing the sim.

This nested-container architecture is supported only on native Linux Docker.
Outer failures to build, start, use the daemon, finish, or produce a safe report
are retry-eligible infrastructure failures rather than candidate failures.

## OpenFIBSEM lift-out evaluator

`fibsem_liftout_v1` is a separate runtime profile, not a PyVISA-derived
instance. It pins OpenFIBSEM commit
`2ebccb8b9721234ca66bb94de36d0f7cfe047af9` and starts a simulator sibling as
UID `11001:11001`; the candidate sibling remains UID `10001:10001`. Candidate
code implements
`run_experiment(microscope, scenario, checkpoint, output_dir) -> dict` and uses
only the public `fibsem_iab` API.

The suite runs `nominal`, four hidden fixed worlds, and five deterministic
seeded worlds. Preflight precedes destructive ROI work. The four semantic
boundaries are `step_1` sample preparation, `step_2` needle-connected source
release, `step_3` target-connected placement, and `step_4` selective needle
separation/retraction. Scoring uses the trusted mesh snapshot and journal to
prove the necessary order; diagnostic candidate JSON cannot establish a gate.

Each step exports `scene.glb`, merged and component STL, SEM/FIB PNG, and
`checkpoint.json`. The outer orchestrator copies validated evidence to
`reports/openfibsem/fibsem_liftout_v1.artifacts/{world_id}/{step_id}/`.
The top-level FIBSEM report is schema version 4 and is published at
`reports/openfibsem/fibsem_liftout_v1.json`. Infrastructure failures
are retryable; state/order/safety/security failures are candidate outcomes.
Passing applies only to the pinned simulation and does not establish physical
microscope safety.

Run local tests with:

```bash
PYTHONPATH=vendor/pyvisa-sim-iab:. python -m pytest -q
```

Default pytest collection intentionally covers the repository-owned `tests/`
and `sources/` trees, not the copied upstream vendor suite. The vendored
random-response compatibility case remains an explicit deterministic gate
(the test resets its random seed for every parameter):

```bash
PYTHONPATH=vendor/pyvisa-sim-iab:. python -m pytest \
  'vendor/pyvisa-sim-iab/pyvisa_sim/testsuite/test_all.py::test_multiple_outputs[ASRL5::INSTR]' \
  -q
```

Native Linux Docker checks are opt-in locally and mandatory in CI:

```bash
IAB_RUN_DOCKER_TESTS=1 python -m unittest \
  tests.integration.test_container_image_linux \
  tests.integration.test_container_isolation_linux \
  tests.integration.test_docker_full_suite_linux \
  tests.integration.test_v2_dual_container_linux -v
```

The native Linux FIBSEM gates are separately opt-in:

```bash
IAB_RUN_FIBSEM_DOCKER_TESTS=1 python -m pytest \
  tests/integration/test_fibsem_dual_container_linux.py \
  tests/integration/test_fibsem_full_suite_linux.py -q
```

Run the portable native-Linux acceptance driver with Python 3.11 and all
repositories mounted at an identical absolute path visible to the host Docker
daemon:

```bash
INSTANCES_REPO_PATH=/absolute/instrument-benchmark-instances \
EVALUATOR_REPO_PATH=/absolute/instrument-benchmark-evaluator \
  scripts/run_fibsem_linux_acceptance.sh \
  /absolute/instrument-benchmark/configs/openfibsem/fibsem_liftout_v1.yaml
```
