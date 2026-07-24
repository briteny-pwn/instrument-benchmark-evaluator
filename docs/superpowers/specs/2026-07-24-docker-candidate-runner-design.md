# Docker-Isolated Candidate Runner Design

Date: 2026-07-24

## Decision

The benchmark will use a host-owned evaluator with a Docker-isolated candidate:

- the Linux host evaluator owns PyVISA/pyvisa-sim, all five instruments,
  `DUTWorld`, the gateway journal, hidden worlds, oracle, constraints, scoring,
  and forced cleanup;
- each candidate execution runs in a fresh Docker container;
- the instance repository supplies the trusted Dockerfile and image lock;
- the candidate receives only instance-visible material, its submission, a
  run-scoped gateway socket, and one writable output directory;
- evaluator evidence is never read from the candidate container.

The first implementation targets native Linux Docker Engine in CI. Docker
Desktop compatibility is outside the first version.

## Feasibility

The change is highly feasible because the current evaluator already:

- creates a new `DUTWorld`, instrument rack, journal, and Unix gateway per world;
- executes the candidate through one replaceable `invoke_candidate()` boundary;
- captures the hidden final snapshot before grading;
- distinguishes candidate status from evaluator infrastructure status;
- force-closes the gateway and rack after candidate termination.

The principal change is replacing the host `subprocess.Popen()` candidate
runner with a Docker runner. Instrument models, worlds, oracle, partial-order
constraints, scoring, and aggregation remain host-owned.

## Trust boundaries

### Trusted

- instrument orchestrator checkout;
- instance repository and its Dockerfile;
- evaluator repository;
- pinned base image digest;
- Docker daemon and Linux host;
- evaluator-created bootstrap and gateway.

### Untrusted

- candidate submission;
- candidate stdout and stderr;
- candidate-generated result;
- all processes created inside the candidate container.

The candidate source is mounted only at runtime and is never part of the image
build context.

## Instance container contract

`instance.yaml` adds:

```yaml
container:
  protocol_version: 1
  dockerfile: Dockerfile
  lock_file: image.lock.yaml
  platform: linux/amd64
  user: "10001:10001"
  workdir: /workspace
  entrypoint:
    - python
    - /runner/bootstrap.py
  gateway_path: /run/iab/gateway.sock
  output_path: /output/result.json
  limits:
    cpus: 1.0
    memory_mb: 512
    pids: 64
    timeout_seconds: 30
    stdout_bytes: 1048576
    stderr_bytes: 1048576
```

The evaluator enforces limits no weaker than its own configured maximums.

`image.lock.yaml` records:

- schema version;
- Dockerfile SHA-256;
- base image reference and immutable digest;
- expected platform;
- built image digest;
- container protocol version.

The Dockerfile must:

- pin every base image by digest;
- create and use UID/GID 10001;
- contain no candidate source;
- contain no evaluator, simulator, oracle, or hidden world;
- require no runtime network access;
- define only the language/runtime dependencies allowed for the task.

The evaluator rejects remote `ADD`, unpinned `FROM`, privileged configuration,
host namespace sharing, device mappings, Docker socket mounts, and root runtime
users.

Image building uses BuildKit with no build network, secrets, SSH forwarding,
or host bind mounts. The instance repository is trusted, but these restrictions
make the built artifact reproducible and auditable.

## Runtime topology

```text
Linux host evaluator
├── InstrumentRack
├── DUTWorld
├── EventJournal
├── Oracle / constraints / scoring
└── /run/iab/<token>/gateway.sock
                  │
                  │ bind-mounted socket only
                  ▼
Candidate container
├── /workspace/instance       read-only
├── /workspace/solution.py    read-only
├── /runner/bootstrap.py      read-only
├── /run/iab/gateway.sock     socket mount
├── /output                   writable bounded directory
└── /tmp                      bounded tmpfs
```

The container runs with `--network none`. It does not need TCP access to the
host. On native Linux, the run-scoped Unix socket is exposed through a narrowly
scoped bind mount. The evaluator creates the socket directory with permissions
that allow UID/GID 10001 to connect but not replace the socket.

The mounted socket implements only the existing versioned VISA gateway
protocol. It is not the Docker daemon socket and conveys no Docker authority.

## Required Docker restrictions

Every candidate container uses:

```text
--network none
--read-only
--user 10001:10001
--cap-drop ALL
--security-opt no-new-privileges
--pids-limit 64
--memory 512m
--memory-swap 512m
--cpus 1.0
--ulimit nofile=256:256
--stop-timeout 1
```

The evaluator also:

- creates a random container name and run token;
- applies labels for evaluator ID, run ID, world ID, and expiry;
- supplies bounded tmpfs mounts only for `/tmp` and necessary runtime paths;
- never mounts Git metadata, evaluator files, hidden data, host Python paths,
  devices, PID/IPC namespaces, or `/var/run/docker.sock`;
- removes the container in a `finally` path;
- performs scoped stale-container cleanup using evaluator-owned labels only.

## Container runner components

```text
instrument_benchmark_evaluator/container/
├── contracts.py
├── image.py
├── runner.py
├── evidence.py
└── errors.py
```

Responsibilities:

- `contracts.py`: parse and validate instance container configuration and
  effective limits;
- `image.py`: validate Dockerfile/lock hashes, build or resolve the image, and
  verify the resulting digest/platform/user;
- `runner.py`: create, start, stream, wait, kill, inspect, and remove one
  candidate container;
- `evidence.py`: normalize immutable Docker runtime metadata and artifact
  hashes;
- `errors.py`: typed invalid-contract, candidate, and infrastructure outcomes.

The Docker CLI is invoked with an argument vector, never through a shell.

## Per-world state machine

```text
VALIDATE_CONTRACT
→ RESOLVE_IMAGE
→ CREATE_RUN_DIRECTORY
→ CREATE_HOST_WORLD
→ START_HOST_GATEWAY
→ CREATE_CONTAINER
→ START_CONTAINER
→ STREAM_AND_WAIT
→ INSPECT_CONTAINER
→ COLLECT_RESULT
→ SNAPSHOT_HOST_STATE
→ FORCE_SAFE_CLEANUP
→ REMOVE_CONTAINER
→ ORACLE_AND_GRADE
```

Every state after container creation has a deterministic cleanup transition.
Container removal never occurs before inspect data, stdout/stderr, result
artifact, and host final-state evidence have been captured.

Each fixed or repeated world receives:

- a fresh container;
- a fresh gateway socket and token;
- a fresh candidate workspace and output directory;
- a fresh instrument rack, journal, and DUT world.

No container, Python module, session, output, or simulated instrument state is
reused between worlds.

## Outcome classification

| Condition | Status |
|---|---|
| Zero exit and schema-valid matching result | `completed` |
| Non-zero candidate/bootstrap exit | `candidate_failure` |
| Evaluator timeout and forced kill | `candidate_timeout` |
| Docker inspect reports OOM kill | `candidate_oom` |
| stdout or stderr exceeds configured cap | `output_limit` |
| Missing, oversized, malformed, or mismatched result | `invalid_result` |
| Candidate/container contract violates policy | `invalid_submission` |
| Docker build/daemon/create/inspect failure | `infrastructure_failure` |
| Host gateway/evaluator failure | `infrastructure_failure` |

Infrastructure failures invalidate the world and are eligible for
infrastructure retry. They do not count as candidate capability failures.

## Evidence model

### Docker daemon evidence

- image reference, ID, and digest;
- Dockerfile and lock SHA-256;
- container ID and normalized create configuration;
- create/start/finish timestamps;
- exit code and OOM status;
- network mode, read-only root, effective user, capabilities, security options,
  limits, namespaces, and normalized mounts;
- container cleanup outcome.

### Container artifact evidence

- candidate SHA-256;
- visible bundle manifest digest;
- result SHA-256 and validated size;
- stdout/stderr byte counts and SHA-256;
- bootstrap classification.

### Host instrument evidence

- append-only gateway journal and hash chain;
- raw request/response hashes;
- pre/post semantic state digests;
- device discovery, identification, configuration, acquisition, and active
  close evidence;
- DUTWorld and five-device final snapshot;
- candidate versus forced cleanup source;
- oracle reconstruction and causal constraint evidence.

The final report adds a `container_evidence` object and a
`container_runtime` evidence-confidence component. Evidence confidence remains
independent of the capability score.

The container never supplies authoritative instrument state. Candidate result
claims are checked only against host evidence.

## Output handling

The only persistent writable mount is a run-scoped output directory. The
evaluator accepts only the declared `result.json`:

- no symlink;
- regular file only;
- bounded size;
- owner/mode checked;
- opened without following links;
- parsed and validated after the container stops;
- hashed before use.

Other output files are ignored or rejected according to the instance policy.
The output directory is removed after report construction.

stdout and stderr are streamed with independent hard byte limits. Exceeding a
limit terminates the candidate and records truncated output plus the full
observed byte count.

## Test requirements

### Contract and image tests

- pinned base digest accepted; floating tags rejected;
- Dockerfile/lock hash mismatch rejected;
- remote `ADD`, root user, forbidden mount or namespace rejected;
- candidate absent from build context;
- built digest/platform/user match the lock.

### Isolation tests

- candidate has no network interface capable of egress;
- DNS and outbound TCP fail;
- evaluator, hidden worlds, simulator, journal, oracle, Git metadata, host
  Python environment, devices, and Docker socket are unreadable;
- candidate can connect to exactly its run-scoped gateway socket;
- non-root UID can connect but cannot replace or unlink the socket;
- root filesystem is read-only and only declared tmpfs/output paths are
  writable.

### Outcome tests

- successful result;
- non-zero crash;
- timeout and forced kill;
- OOM classification;
- stdout and stderr limits;
- missing, malformed, oversized, symlinked, and return-value-mismatched result;
- Docker daemon unavailable and image build failure as infrastructure failure.

### Evidence and safety tests

- candidate cannot modify the host journal;
- inspect configuration matches the declared security policy;
- unique container/socket IDs per world;
- unsafe or killed candidate still yields a host final snapshot;
- forced cleanup turns AWG and PSU off, opens routes, and closes sessions;
- candidate-active cleanup remains distinguishable from forced cleanup;
- candidate measurement claims are checked against host raw observations.

### End-to-end tests

- reference candidate strict-passes nine fixed and ten repeated worlds;
- all adversarial candidates fail their intended gates;
- two clean runs have identical semantic reports and image digest;
- no container, socket, session, world, or output state leaks between runs.

Docker-dependent tests use an explicit integration marker and fail with a clear
infrastructure prerequisite message when Docker is unavailable; they are not
silently skipped in the official validation workflow.

## Migration sequence

1. Add the instance Dockerfile, lock, manifest contract, and instance tests.
2. Add evaluator container contracts and Dockerfile policy validation.
3. Implement image resolution/build evidence.
4. Implement the Docker runner behind the existing candidate-runner interface.
5. Preserve the current host subprocess runner temporarily as a test-only
   reference backend.
6. Add Docker outcome, isolation, and cleanup tests.
7. Switch official evaluator worlds to the Docker backend.
8. Extend schemas, scoring confidence, reports, documentation, and distributed
   validation.
9. Run the full benchmark on a clean native Linux CI worker.

## Explicit limitations

- The first version targets native Linux Docker Engine, not Docker Desktop.
- Docker daemon and host kernel are trusted infrastructure.
- Simulator success still does not prove physical-instrument transfer.
- A later production deployment may additionally use rootless Docker or a
  dedicated ephemeral worker, but this is not required for the first version.
