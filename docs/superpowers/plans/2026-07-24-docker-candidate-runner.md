# Docker-Isolated Candidate Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run every benchmark candidate inside a fresh, resource-limited, network-disabled Docker container while the Linux host evaluator retains authoritative instrument state, evidence, oracle, scoring, and safety cleanup.

**Architecture:** The instance repository defines and locks a non-root candidate image. The evaluator validates that contract, builds or resolves the image, bind-mounts a run-scoped Unix gateway socket, executes one fresh container per world, and combines Docker evidence with host-owned instrument evidence. The existing host subprocess backend remains test-only until Docker parity and end-to-end tests pass.

**Tech Stack:** Python 3.11+, Docker Engine/BuildKit on native Linux, PyYAML 6.0.3, PyVISA 1.16.2, pyvisa-sim 0.7.1, Unix domain sockets, JSON, unittest.

## Global Constraints

- Target native Linux Docker Engine first; do not claim Docker Desktop support.
- Docker daemon, Linux kernel, evaluator, instance repository, and pinned image digest are trusted.
- Candidate code and all candidate-created processes/artifacts are untrusted.
- Candidate source is mounted at runtime and never enters the Docker build context.
- Host evaluator remains the sole authority for instrument state, gateway journal, oracle, constraints, scoring, and forced cleanup.
- Container network mode is exactly `none`.
- Container runs as `10001:10001`, with read-only root, all capabilities dropped, and `no-new-privileges`.
- Default hard limits are 1 CPU, 512 MiB memory and swap, 64 PIDs, 256 file descriptors, and 30 seconds.
- The only persistent writable mount is the bounded run output directory; `/tmp` and runtime scratch use bounded tmpfs.
- Never mount evaluator files, hidden worlds, simulator YAML, journal, oracle, Git metadata, host Python paths, devices, PID/IPC namespaces, or `/var/run/docker.sock`.
- Each world receives a unique container, gateway socket, run directory, instrument rack, journal, and DUT world.
- Docker infrastructure failures invalidate the world; they never count as candidate capability failures.
- All Docker commands use argument arrays with `shell=False`.
- Preserve current host subprocess runner only as a test backend; official evaluation must use Docker after the migration gate.

---

### Task 1: Extend the instance container contract

**Repository:** `/Users/britenyyyang/benchmark/instance`

**Files:**
- Modify: `pyvisa_dut_validation_v1/instance.yaml`
- Modify: `schemas/instance.schema.json`
- Modify: `tests/test_instance.py`
- Create: `pyvisa_dut_validation_v1/Dockerfile`
- Create: `pyvisa_dut_validation_v1/image.lock.yaml`
- Modify: `README.md`

**Interfaces:**
- Consumes: instance schema version 1 and current visible-file SHA-256 manifest.
- Produces: `container` manifest object and locked image inputs consumed by evaluator `load_container_contract()`.

- [ ] **Step 1: Add failing manifest tests**

Add assertions to `tests/test_instance.py`:

```python
def test_container_contract_is_linux_non_root_and_networkless(self) -> None:
    container = self.manifest["container"]
    self.assertEqual(container["protocol_version"], 1)
    self.assertEqual(container["platform"], "linux/amd64")
    self.assertEqual(container["user"], "10001:10001")
    self.assertEqual(container["gateway_path"], "/run/iab/gateway.sock")
    self.assertEqual(container["output_path"], "/output/result.json")
    self.assertEqual(container["limits"]["memory_mb"], 512)
    self.assertEqual(container["limits"]["pids"], 64)

def test_dockerfile_and_lock_are_hashed_visible_build_inputs(self) -> None:
    container = self.manifest["container"]
    for name in (container["dockerfile"], container["lock_file"]):
        path = INSTANCE / name
        self.assertTrue(path.is_file())
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            container["context_files"][name],
        )
```

- [ ] **Step 2: Run the tests and observe missing container contract**

Run:

```bash
cd /Users/britenyyyang/benchmark/instance
../.venv/bin/python -m unittest tests.test_instance -v
```

Expected: the two new tests fail with missing `container`, `Dockerfile`, or lock data.

- [ ] **Step 3: Add the locked non-root image**

Create a Dockerfile with a digest-pinned Python slim base, UID/GID 10001, no
candidate copy, and:

```dockerfile
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
RUN groupadd --gid 10001 benchmark \
 && useradd --uid 10001 --gid 10001 --create-home benchmark
WORKDIR /workspace
USER 10001:10001
ENTRYPOINT ["python", "/runner/bootstrap.py"]
```

Record the exact base digest and Dockerfile hash in `image.lock.yaml`. Add the
complete `container` object from the design spec to `instance.yaml`; update
`context_files` after final Dockerfile/lock bytes are fixed.

- [ ] **Step 4: Extend the instance schema**

Require `container` and validate exact keys, positive limits, fixed UID/GID,
absolute gateway/output paths, and `platform: linux/amd64`. Keep
`additionalProperties: false`.

- [ ] **Step 5: Run instance tests**

Run:

```bash
../.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

Expected: all instance tests pass and no whitespace errors are reported.

- [ ] **Step 6: Commit the instance contract**

```bash
git add README.md schemas/instance.schema.json tests/test_instance.py \
  pyvisa_dut_validation_v1/instance.yaml \
  pyvisa_dut_validation_v1/Dockerfile \
  pyvisa_dut_validation_v1/image.lock.yaml
git commit -m "feat: define locked candidate container"
```

### Task 2: Add evaluator container contract types and policy validation

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Create: `instrument_benchmark_evaluator/container/__init__.py`
- Create: `instrument_benchmark_evaluator/container/contracts.py`
- Create: `instrument_benchmark_evaluator/container/errors.py`
- Create: `tests/test_container_contracts.py`
- Modify: `instrument_benchmark_evaluator/contracts.py`

**Interfaces:**
- Consumes: instance `container` and `image.lock.yaml`.
- Produces:
  - `ContainerLimits`;
  - `ContainerContract`;
  - `ImageLock`;
  - `EffectiveContainerPolicy`;
  - `load_container_contract(instance_path: Path) -> ContainerContract`;
  - `effective_policy(contract, evaluator_maxima) -> EffectiveContainerPolicy`.

- [ ] **Step 1: Write failing contract tests**

Cover valid parsing and explicit rejection of floating image tags, wrong
protocol/platform/user, non-absolute container paths, hash mismatch, zero or
negative limits, and limits above evaluator maxima:

```python
def test_effective_policy_never_exceeds_evaluator_maximum(self) -> None:
    policy = effective_policy(
        contract_with(cpus=4.0, memory_mb=4096, pids=512),
        EvaluatorMaxima(cpus=1.0, memory_mb=512, pids=64),
    )
    self.assertEqual(policy.cpus, 1.0)
    self.assertEqual(policy.memory_mb, 512)
    self.assertEqual(policy.pids, 64)
```

- [ ] **Step 2: Run tests and verify missing modules**

```bash
cd /Users/britenyyyang/benchmark/evaluator
../.venv/bin/python -m unittest tests.test_container_contracts -v
```

Expected: import failure for `instrument_benchmark_evaluator.container`.

- [ ] **Step 3: Implement immutable dataclasses and typed errors**

Define `ContainerContractError`, `ImagePolicyError`, and
`ContainerInfrastructureError`. Parse YAML with exact key checks, resolve all
paths under the instance root, compare SHA-256 values, and reject path escape.

- [ ] **Step 4: Make evaluator request loading include the container contract**

Extend `InstanceSettings` with:

```python
container: ContainerContract
```

Call `load_container_contract()` from `load_instance_settings()`.

- [ ] **Step 5: Run contract and existing evaluator tests**

```bash
../.venv/bin/python -m unittest tests.test_container_contracts tests.test_cli -v
../.venv/bin/python -m unittest discover \
  -s evaluators/pyvisa_dut_validation_v1/tests -v
```

Expected: all tests pass; existing evaluator behavior is unchanged.

- [ ] **Step 6: Commit contract validation**

```bash
git add instrument_benchmark_evaluator/container \
  instrument_benchmark_evaluator/contracts.py \
  tests/test_container_contracts.py
git commit -m "feat: validate candidate container contracts"
```

### Task 3: Validate Dockerfile policy without building

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Create: `instrument_benchmark_evaluator/container/dockerfile.py`
- Create: `tests/test_dockerfile_policy.py`
- Create: `tests/fixtures/dockerfiles/valid.Dockerfile`
- Create: `tests/fixtures/dockerfiles/floating.Dockerfile`
- Create: `tests/fixtures/dockerfiles/remote_add.Dockerfile`
- Create: `tests/fixtures/dockerfiles/root.Dockerfile`

**Interfaces:**
- Consumes: Dockerfile bytes, `ImageLock`, `ContainerContract`.
- Produces: `validate_dockerfile(path, lock, contract) -> DockerfileEvidence`.

- [ ] **Step 1: Write a table-driven failing policy test**

```python
CASES = {
    "floating.Dockerfile": "digest-pinned",
    "remote_add.Dockerfile": "remote ADD",
    "root.Dockerfile": "USER 10001:10001",
}

def test_invalid_dockerfiles_are_rejected(self) -> None:
    for filename, message in CASES.items():
        with self.subTest(filename=filename):
            with self.assertRaisesRegex(ImagePolicyError, message):
                validate_dockerfile(FIXTURES / filename, self.lock, self.contract)
```

- [ ] **Step 2: Run the test and verify failure**

```bash
../.venv/bin/python -m unittest tests.test_dockerfile_policy -v
```

Expected: import failure for `container.dockerfile`.

- [ ] **Step 3: Implement a conservative line-oriented validator**

Reject:

- every `FROM` that does not resolve to `name@sha256:<64 hex>`;
- remote URL sources in `ADD`;
- missing final `USER 10001:10001`;
- candidate/evaluator/hidden file names in `COPY` or `ADD`;
- lock Dockerfile hash mismatch;
- unsupported instruction continuations that cannot be parsed unambiguously.

Return:

```python
DockerfileEvidence(
    dockerfile_sha256: str,
    base_images: tuple[str, ...],
    final_user: str,
)
```

- [ ] **Step 4: Run policy tests**

```bash
../.venv/bin/python -m unittest tests.test_dockerfile_policy -v
```

Expected: valid fixture passes and all malicious fixtures fail for their named reason.

- [ ] **Step 5: Commit**

```bash
git add instrument_benchmark_evaluator/container/dockerfile.py \
  tests/test_dockerfile_policy.py tests/fixtures/dockerfiles
git commit -m "feat: enforce Dockerfile security policy"
```

### Task 4: Add a replaceable Docker command client

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Create: `instrument_benchmark_evaluator/container/docker_client.py`
- Create: `tests/test_docker_client.py`

**Interfaces:**
- Produces:
  - `DockerCommandResult`;
  - `DockerClient.run(arguments, timeout=None)`;
  - `DockerClient.inspect(container_id)`;
  - `DockerClient.image_inspect(image_ref)`;
  - `DockerClient.remove(container_id)`.

- [ ] **Step 1: Write tests with a recording executor**

```python
def test_commands_are_argument_vectors_without_shell(self) -> None:
    executor = RecordingExecutor()
    DockerClient(executor=executor).inspect("abc")
    self.assertEqual(executor.calls[0].argv, ["docker", "inspect", "abc"])
    self.assertFalse(executor.calls[0].shell)
```

Also test timeout, missing Docker binary, non-zero daemon errors, malformed
inspect JSON, and output byte caps.

- [ ] **Step 2: Run tests and verify failure**

```bash
../.venv/bin/python -m unittest tests.test_docker_client -v
```

- [ ] **Step 3: Implement the minimal client**

Use `subprocess.run(..., shell=False, stdin=DEVNULL, stdout=PIPE,
stderr=PIPE)` and convert Docker invocation failures to
`ContainerInfrastructureError`. Do not classify candidate exit codes here.

- [ ] **Step 4: Run tests**

```bash
../.venv/bin/python -m unittest tests.test_docker_client -v
```

- [ ] **Step 5: Commit**

```bash
git add instrument_benchmark_evaluator/container/docker_client.py \
  tests/test_docker_client.py
git commit -m "feat: add typed Docker command client"
```

### Task 5: Implement reproducible image resolution and build evidence

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Create: `instrument_benchmark_evaluator/container/image.py`
- Create: `tests/test_container_image.py`
- Create: `tests/integration/test_container_image_linux.py`

**Interfaces:**
- Consumes: validated `ContainerContract`, `ImageLock`, `DockerClient`.
- Produces:
  - `ImageEvidence`;
  - `resolve_image(contract, client) -> ImageEvidence`;
  - `build_image(contract, client) -> ImageEvidence`.

- [ ] **Step 1: Write unit tests for inspect normalization**

Use fixture inspect JSON to assert exact digest, platform, architecture, user,
labels, and Dockerfile hash. Reject missing digest, wrong platform/user, or
lock mismatch.

- [ ] **Step 2: Run unit tests and verify failure**

```bash
../.venv/bin/python -m unittest tests.test_container_image -v
```

- [ ] **Step 3: Implement resolve/build**

Build with argument vector equivalent to:

```text
docker buildx build
--load
--network none
--platform linux/amd64
--build-arg PYTHON_IMAGE=<digest-pinned-ref>
--label iab.instance=<instance-id>
--label iab.dockerfile-sha256=<sha>
-f <Dockerfile>
<minimal-context-directory>
```

Create the build context in evaluator temporary storage and copy only declared
container context files. Candidate and visible task files must be absent.

- [ ] **Step 4: Add Linux integration test**

Build the real instance image and inspect it:

```python
@requires_linux_docker
def test_real_instance_image_matches_lock(self) -> None:
    evidence = build_image(load_real_contract(), DockerClient())
    self.assertEqual(evidence.platform, "linux/amd64")
    self.assertEqual(evidence.user, "10001:10001")
```

- [ ] **Step 5: Run unit and Docker integration tests**

```bash
../.venv/bin/python -m unittest tests.test_container_image -v
../.venv/bin/python -m unittest tests.integration.test_container_image_linux -v
```

Expected on official Linux CI: both pass. Missing Docker must produce a clear
infrastructure prerequisite failure rather than a skip.

- [ ] **Step 6: Commit**

```bash
git add instrument_benchmark_evaluator/container/image.py \
  tests/test_container_image.py tests/integration/test_container_image_linux.py
git commit -m "feat: resolve reproducible candidate images"
```

### Task 6: Split the candidate bootstrap from the host filesystem

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Modify: `instrument_benchmark_evaluator/bootstrap.py`
- Create: `instrument_benchmark_evaluator/container/bootstrap_contract.py`
- Create: `tests/test_container_bootstrap.py`

**Interfaces:**
- Container invocation:

```text
python /runner/bootstrap.py
  /workspace/solution.py
  /run/iab/gateway.sock
  /output/result.json
  /output/return.json
```

- Exit codes: 0 valid, 1 candidate exception, 2 invalid entrypoint, 3 invalid result.

- [ ] **Step 1: Write bootstrap tests in a temporary filesystem**

Cover valid result, missing entrypoint, exception, missing result, malformed
JSON, and returned/written mismatch.

- [ ] **Step 2: Run tests and observe host-specific assumptions**

```bash
../.venv/bin/python -m unittest tests.test_container_bootstrap -v
```

- [ ] **Step 3: Refactor bootstrap**

Remove assumptions about the evaluator workspace path and host audit roots.
Keep candidate import restrictions inside the image, and accept only the four
explicit absolute container paths. The bootstrap must not know hidden world or
scoring information.

- [ ] **Step 4: Run tests**

```bash
../.venv/bin/python -m unittest tests.test_container_bootstrap -v
```

- [ ] **Step 5: Commit**

```bash
git add instrument_benchmark_evaluator/bootstrap.py \
  instrument_benchmark_evaluator/container/bootstrap_contract.py \
  tests/test_container_bootstrap.py
git commit -m "refactor: make candidate bootstrap container-safe"
```

### Task 7: Implement secure output collection and container evidence

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Create: `instrument_benchmark_evaluator/container/evidence.py`
- Create: `instrument_benchmark_evaluator/container/output.py`
- Create: `tests/test_container_evidence.py`
- Create: `tests/test_container_output.py`

**Interfaces:**
- Produces:
  - `ContainerEvidence`;
  - `ArtifactEvidence`;
  - `normalize_inspect(value) -> ContainerEvidence`;
  - `collect_result(output_dir, filename, max_bytes) -> CollectedResult`.

- [ ] **Step 1: Write inspect evidence tests**

Assert normalized network mode, rootfs readonly, user, caps, security options,
limits, namespaces, mounts, timestamps, exit code, OOM state, image digest, and
cleanup status.

- [ ] **Step 2: Write output attack tests**

Cover regular JSON, missing file, directory, FIFO, symlink, oversized file,
malformed JSON, mismatched return artifact, wrong owner/mode, and additional
files under a strict-output policy.

- [ ] **Step 3: Run tests and verify missing implementation**

```bash
../.venv/bin/python -m unittest \
  tests.test_container_evidence tests.test_container_output -v
```

- [ ] **Step 4: Implement no-follow bounded collection**

Use `os.open()` with `O_RDONLY | O_NOFOLLOW`, `fstat()`, bounded reads, SHA-256,
and regular-file checks. Normalize inspect fields into immutable dataclasses.

- [ ] **Step 5: Run tests**

```bash
../.venv/bin/python -m unittest \
  tests.test_container_evidence tests.test_container_output -v
```

- [ ] **Step 6: Commit**

```bash
git add instrument_benchmark_evaluator/container/evidence.py \
  instrument_benchmark_evaluator/container/output.py \
  tests/test_container_evidence.py tests/test_container_output.py
git commit -m "feat: collect trusted container evidence"
```

### Task 8: Implement the Docker runner state machine

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Create: `instrument_benchmark_evaluator/container/runner.py`
- Create: `tests/test_container_runner.py`
- Create: `tests/fixtures/docker_inspect/*.json`

**Interfaces:**
- Consumes:
  - `ContainerContract`;
  - `EffectiveContainerPolicy`;
  - `ImageEvidence`;
  - prepared visible workspace;
  - candidate path;
  - host gateway socket;
  - `DockerClient`.
- Produces:

```python
ContainerProcessResult(
    status: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
    result: dict[str, Any] | None,
    container_evidence: ContainerEvidence,
    artifact_evidence: ArtifactEvidence,
)
```

- [ ] **Step 1: Write state-machine tests using a fake Docker client**

Verify exact create flags and transitions for completed, crash, timeout, OOM,
output limit, invalid result, create failure, start failure, inspect failure,
and remove failure.

The create command test must assert all required restrictions:

```python
self.assertIn("--network=none", argv)
self.assertIn("--read-only", argv)
self.assertIn("--cap-drop=ALL", argv)
self.assertIn("--security-opt=no-new-privileges", argv)
self.assertNotIn("--privileged", argv)
self.assertNotIn("/var/run/docker.sock", " ".join(argv))
```

- [ ] **Step 2: Run and verify failure**

```bash
../.venv/bin/python -m unittest tests.test_container_runner -v
```

- [ ] **Step 3: Implement deterministic lifecycle**

Implement create → start → stream/wait → inspect → collect → remove with one
`try/finally` cleanup path. Inspect must happen before removal. On timeout,
issue `docker kill`, wait, inspect, then remove. Preserve remove errors in
evidence without overwriting the original candidate classification.

- [ ] **Step 4: Run state-machine tests**

```bash
../.venv/bin/python -m unittest tests.test_container_runner -v
```

- [ ] **Step 5: Commit**

```bash
git add instrument_benchmark_evaluator/container/runner.py \
  tests/test_container_runner.py tests/fixtures/docker_inspect
git commit -m "feat: run candidates in hardened containers"
```

### Task 9: Prove Linux socket, network, filesystem, and resource isolation

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Create: `tests/integration/test_container_isolation_linux.py`
- Create: `tests/fixtures/candidates/probe_isolation.py`
- Create: `tests/fixtures/candidates/fill_memory.py`
- Create: `tests/fixtures/candidates/fork_bomb_guarded.py`

**Interfaces:**
- Consumes: real Docker Engine, real instance image, run-scoped host gateway.
- Produces: executable proof of the container trust boundary.

- [ ] **Step 1: Add a probe candidate**

The candidate writes a result describing whether it could:

- resolve DNS or connect outbound;
- read evaluator paths, Git metadata, host Python paths, devices, or Docker socket;
- write outside `/output` and declared tmpfs;
- unlink or replace the gateway socket;
- connect to the gateway and list resources.

- [ ] **Step 2: Add Linux Docker integration assertions**

Assert all forbidden probes fail, gateway access succeeds, effective UID is
10001, network mode is `none`, and inspect reports every required restriction.

- [ ] **Step 3: Add resource/outcome tests**

Run bounded memory and process-pressure candidates; assert OOM and PID behavior
without affecting the host evaluator. Add stdout/stderr and timeout cases.

- [ ] **Step 4: Run Linux isolation suite**

```bash
../.venv/bin/python -m unittest \
  tests.integration.test_container_isolation_linux -v
```

Expected on official Linux CI: all isolation assertions pass. Any missing
Docker prerequisite fails before candidate tests start.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_container_isolation_linux.py \
  tests/fixtures/candidates
git commit -m "test: prove candidate container isolation"
```

### Task 10: Integrate Docker backend with host-owned world execution

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Modify: `instrument_benchmark_evaluator/run.py`
- Rename: `instrument_benchmark_evaluator/submission.py` to `instrument_benchmark_evaluator/host_submission.py`
- Create: `instrument_benchmark_evaluator/candidate_backend.py`
- Modify: `instrument_benchmark_evaluator/contracts.py`
- Modify: `tests/test_cli.py`
- Create: `tests/test_run_backend.py`

**Interfaces:**
- Produces:

```python
class CandidateBackend(Protocol):
    def invoke(
        self,
        *,
        workspace: Path,
        candidate_path: Path,
        endpoint: Path,
        instance: InstanceSettings,
        timeout_seconds: float,
        max_output_bytes: int,
        run_id: str,
        world_id: str,
    ) -> CandidateProcessResult: ...
```

- Official CLI constructs `DockerCandidateBackend`; unit tests may inject
  `HostCandidateBackend`.

- [ ] **Step 1: Write failing backend selection tests**

Assert CLI defaults to Docker, no production flag selects host execution, and
Docker infrastructure failure returns evaluator exit 3 rather than a candidate
world failure.

- [ ] **Step 2: Write a safety regression test**

Use a fake backend that exits after turning sources on. Assert `run_world()`
captures the unsafe host snapshot, records forced cleanup, and leaves the rack
safe after `finally`.

- [ ] **Step 3: Run tests and verify current direct invocation**

```bash
../.venv/bin/python -m unittest tests.test_run_backend tests.test_cli -v
```

- [ ] **Step 4: Introduce the backend protocol**

Move the current subprocess implementation to `HostCandidateBackend` and mark
it test-only. Inject `CandidateBackend` into `run_world()` and
`run_full_suite()`. Construct `DockerCandidateBackend` in CLI after image
resolution.

- [ ] **Step 5: Preserve host evidence ownership**

Keep `InstrumentRack`, `EventJournal`, `GatewayServer`, final snapshot,
`grade_run()`, and forced cleanup in `run_world()`. Do not pass journal, world,
or oracle paths to the backend.

- [ ] **Step 6: Run unit and evaluator suites**

```bash
../.venv/bin/python -m unittest discover -s tests -v
../.venv/bin/python -m unittest discover \
  -s evaluators/pyvisa_dut_validation_v1/tests -v
```

- [ ] **Step 7: Commit**

```bash
git add instrument_benchmark_evaluator evaluators tests
git commit -m "refactor: execute official worlds through Docker"
```

### Task 11: Extend report schema, status aggregation, and confidence

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Modify: `evaluators/pyvisa_dut_validation_v1/scoring.py`
- Modify: `report.schema.json`
- Modify: `tests/test_cli.py`
- Modify: `evaluators/pyvisa_dut_validation_v1/tests/test_scoring.py`
- Create: `tests/test_container_reporting.py`

**Interfaces:**
- Adds `container_evidence`, `artifact_evidence`, and
  `evidence_confidence.container_runtime` to each world report.
- Adds aggregate infrastructure validity and retry eligibility without changing
  capability score weights.

- [ ] **Step 1: Write failing report tests**

Assert:

- Docker security evidence is present per world;
- `container_runtime` confidence is independently computed;
- confidence changes never change capability score;
- infrastructure failures set report validity false and retryable true;
- OOM/timeout remain candidate statuses;
- forced cleanup evidence survives candidate termination.

- [ ] **Step 2: Run tests**

```bash
../.venv/bin/python -m unittest \
  tests.test_container_reporting \
  evaluators.pyvisa_dut_validation_v1.tests.test_scoring -v
```

- [ ] **Step 3: Extend immutable report dataclasses and schema**

Add explicit typed fields rather than unstructured metadata. Preserve the six
capability dimensions totaling 100. Update aggregate strict-pass logic so
infrastructure-invalid worlds cannot silently lower candidate robustness.

- [ ] **Step 4: Run reporting and full local tests**

```bash
../.venv/bin/python -m unittest discover -s tests -v
../.venv/bin/python -m unittest discover \
  -s evaluators/pyvisa_dut_validation_v1/tests -v
```

- [ ] **Step 5: Commit**

```bash
git add report.schema.json \
  evaluators/pyvisa_dut_validation_v1/scoring.py \
  evaluators/pyvisa_dut_validation_v1/tests/test_scoring.py \
  tests/test_cli.py tests/test_container_reporting.py
git commit -m "feat: report container runtime evidence"
```

### Task 12: Run the real reference and adversarial matrix in Docker

**Repository:** `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Modify: `evaluators/pyvisa_dut_validation_v1/tests/test_end_to_end.py`
- Modify: `evaluators/pyvisa_dut_validation_v1/tests/test_adversarial.py`
- Create: `tests/integration/test_docker_full_suite_linux.py`

**Interfaces:**
- Consumes: real instance image and all existing fixed/repeated worlds.
- Produces: Docker-backed parity and adversarial evidence.

- [ ] **Step 1: Add one-world reference parity test**

Run nominal through host test backend and Docker backend; compare oracle,
constraints, capability dimensions, gates, and semantic evidence while ignoring
container IDs and timestamps.

- [ ] **Step 2: Add full Docker suite**

Run nine fixed and ten repeated worlds and assert:

```python
self.assertTrue(report.strict_pass)
self.assertEqual(report.score, 100)
self.assertEqual(report.fixed_world_pass_rate, 1.0)
self.assertEqual(report.repeated_world_pass_rate, 1.0)
self.assertEqual(len(report.worlds), 19)
self.assertEqual(len({w.container_id for w in report.worlds}), 19)
```

- [ ] **Step 3: Run adversarial submissions in Docker**

Preserve the current expected status and failed-gate matrix. Add container
escape probes, unsafe exit, OOM, timeout, result symlink, and output-flood cases.

- [ ] **Step 4: Run full Linux Docker suite twice**

```bash
../.venv/bin/python -m unittest \
  tests.integration.test_docker_full_suite_linux -v
```

Compare semantic reports and image digest; ignore container IDs and timing.
Assert no labeled containers or socket directories remain.

- [ ] **Step 5: Commit**

```bash
git add evaluators/pyvisa_dut_validation_v1/tests \
  tests/integration/test_docker_full_suite_linux.py
git commit -m "test: validate full benchmark in Docker"
```

### Task 13: Update instrument orchestration and distributed validation

**Repositories:**
- `/Users/britenyyyang/benchmark/instrument`
- `/Users/britenyyyang/benchmark/evaluator`

**Files:**
- Modify: `instrument/configs/pyvisa_dut_validation_v1.yaml`
- Modify: `instrument/src/instrument_benchmark/orchestrator.py`
- Modify: `instrument/scripts/validate_distributed_benchmark.py`
- Modify: `instrument/schemas/run.schema.json`
- Modify: `instrument/tests/test_orchestrator.py`
- Modify: `evaluator/evaluator.yaml`

**Interfaces:**
- Instrument request adds Docker policy/image mode but still invokes evaluator
  only through JSON/CLI.
- Final provenance records instance commit, evaluator commit, instrument commit,
  Dockerfile hash, image digest, and Docker Engine version.

- [ ] **Step 1: Add failing fake-evaluator tests**

Assert the evaluator request includes container protocol version and requires
container evidence in the returned report. Reject an evaluator report missing
image digest or Docker security evidence.

- [ ] **Step 2: Run instrument tests**

```bash
cd /Users/britenyyyang/benchmark/instrument
../.venv/bin/python -m unittest discover -s tests -v
```

- [ ] **Step 3: Extend run request and report validation**

Do not import evaluator modules. Add only JSON contract fields. Preserve exact
three-repository Git provenance.

- [ ] **Step 4: Extend distributed validation**

Require Linux, Docker daemon availability, all repository-local tests, Docker
isolation tests, full 19-world Docker run twice, adversarial matrix, zero stale
containers, and semantic reproducibility.

- [ ] **Step 5: Run instrument tests**

```bash
../.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

- [ ] **Step 6: Commit evaluator manifest change**

```bash
cd /Users/britenyyyang/benchmark/evaluator
git add evaluator.yaml
git commit -m "feat: declare Docker evaluator protocol"
```

- [ ] **Step 7: Commit instrument contract change**

```bash
cd /Users/britenyyyang/benchmark/instrument
git add configs schemas src scripts tests
git commit -m "feat: orchestrate Docker-backed evaluation"
```

### Task 14: Add native Linux CI

**Repositories:**
- `/Users/britenyyyang/benchmark/instance`
- `/Users/britenyyyang/benchmark/evaluator`
- `/Users/britenyyyang/benchmark/instrument`

**Files:**
- Create: `instance/.github/workflows/test.yml`
- Create: `evaluator/.github/workflows/test.yml`
- Create: `instrument/.github/workflows/distributed-docker.yml`
- Modify: all three `README.md`

**Interfaces:**
- Produces: repository-local fast checks and one official distributed Docker
  validation workflow on Linux.

- [ ] **Step 1: Add fast workflows**

Instance and evaluator unit jobs use Python 3.11 and pinned project
dependencies. Evaluator separates non-Docker unit tests from Docker integration.

- [ ] **Step 2: Add distributed workflow**

The instrument workflow checks out:

```text
instrument-benchmark           → instrument/ distributed-model
instrument-benchmark-instances → instance/ main
instrument-benchmark-evaluator → evaluator/ main
```

It verifies Docker Engine/BuildKit, installs instrument/evaluator packages,
builds the locked image, and runs
`scripts/validate_distributed_benchmark.py`.

- [ ] **Step 3: Add artifact and cleanup steps**

Always upload the validation report, Docker inspect evidence, and bounded logs.
Always remove containers/images labeled with the workflow run ID. Do not run
global `docker system prune`.

- [ ] **Step 4: Validate workflow syntax and local commands**

Run every command named in each workflow on the Linux worker. Confirm missing
Docker fails the official job rather than skipping integration.

- [ ] **Step 5: Commit each repository**

```bash
git add .github README.md
git commit -m "ci: validate Docker-isolated benchmark"
```

Run separately in instance, evaluator, and instrument.

### Task 15: Final requirement audit, report, and coordinated release

**Repositories:** all three sibling repositories.

**Files:**
- Modify: `instrument/todo.md`
- Modify: `instrument/reports/distributed_validation.json`
- Modify: `evaluator/README.md`
- Modify: `instance/README.md`

**Interfaces:**
- Produces: evidence-backed completion audit and three mutually compatible
  remote commits.

- [ ] **Step 1: Run complete fresh validation on native Linux**

```bash
cd /Users/britenyyyang/benchmark/instrument
PYTHONPATH=src ../.venv/bin/python scripts/validate_distributed_benchmark.py
```

Expected:

- all instance, evaluator, instrument, Docker isolation, and adversarial tests pass;
- 19/19 worlds execute in distinct containers;
- strict pass true and score 100;
- fixed/repeated pass rates 1.0/1.0;
- semantic reproducibility true;
- no stale labeled container/socket/output remains.

- [ ] **Step 2: Audit the report**

Check all command exit codes, image/container evidence, repository SHAs,
per-device state, experiment completion, final safety, cleanup source,
confidence, and declared limitations.

- [ ] **Step 3: Update completion audit**

Mark an item complete only when the validation report or named test directly
proves it. Keep physical-hardware transfer and trusted Docker daemon as explicit
limitations.

- [ ] **Step 4: Run final clean checks**

In each repository:

```bash
git diff --check
git status --short
```

Then rerun repository-local tests after the last documentation/report commit.

- [ ] **Step 5: Commit report and audit**

```bash
cd /Users/britenyyyang/benchmark/instrument
git add todo.md reports/distributed_validation.json
git commit -m "docs: record Docker benchmark validation"
```

- [ ] **Step 6: Push in dependency order**

Push and verify:

1. instance `main`;
2. evaluator `main`;
3. instrument `distributed-model`.

After each push, fetch and assert `HEAD == origin/<branch>`. Never force-push.

## Completion Gate

Implementation is complete only when:

- official candidate execution uses Docker, not the host subprocess backend;
- all three repository worktrees are clean;
- all local and Linux Docker integration tests pass;
- reference and adversarial suites execute in Docker;
- final report records matching repository and image provenance;
- remote branch SHAs match the verified local commits.
