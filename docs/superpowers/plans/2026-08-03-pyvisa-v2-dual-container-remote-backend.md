# PyVISA v2 Dual-Container Remote Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate `pyvisa_dut_validation_v2` into the formal three-repository evaluator so an untrusted candidate explicitly uses `pyvisa.ResourceManager("@iab")` in a networkless candidate container while a separate networkless sim container owns all PyVISA-sim state and hidden evidence.

**Architecture:** The instance repository supplies a candidate-only image containing official PyVISA 1.16.2 and a thin `pyvisa_iab.IABVisaLibrary`. The evaluator image supplies the matching Unix-socket broker, hooked PyVISA-sim 0.7.1, hidden world/DUT, complete hash-chained journal, dual-sibling lifecycle, evidence projection, oracle, and score. The instrument repository builds the evaluator image, injects its exact image ID so the outer evaluator can start a sim sibling, and validates the v2 schema without changing v1.

**Tech Stack:** Python 3.11, PyVISA 1.16.2, hooked PyVISA-sim 0.7.1, AF_UNIX/SOCK_STREAM, length-prefixed canonical JSON, Docker Engine on native Linux, PyYAML 6.0.3, `unittest`, locked offline wheels.

## Global Constraints

- The candidate source must contain the literal `pyvisa.ResourceManager("@iab")`; do not set `PYVISA_LIBRARY` or make `@iab` implicit.
- Forward only `SimVisaLibrary.open_default_resource_manager`, `list_resources`, `open`, `close`, `read`, `write`, `get_attribute`, `set_attribute`, `disable_event`, and `discard_events` with their PyVISA-sim 0.7.1 signatures and return shapes.
- `list_resources()` returns only `tuple[str, ...]`; the backend method is `open()`, while `ResourceManager.open_bare_resource()` remains official frontend code.
- Do not implement enable/wait events, handlers, SRQ, async I/O, trigger, locks, memory access, or bus control.
- Both sim and candidate siblings use `network=none`, read-only roots, fixed UIDs, dropped capabilities, no-new-privileges, bounded resources, and no Docker socket.
- The candidate sees the transport directory read-only and never sees world, evidence, evaluator files, simulator files, or hidden exceptions.
- The sim sees transport/evidence read-write and world read-only; it never sees candidate source/output or the Docker socket.
- Every world uses fresh containers, socket, sessions, journal, and DUT state. The evaluator always removes the candidate before SIGTERM/finalizing the sim.
- Preserve every lifecycle, connection, RPC, session, SCPI, hook, state, rejection, and cleanup event in the raw hash chain. Redaction may replace secret values with type/length/digest but may not omit events.
- Candidate-originated malformed traffic is non-retryable. Only trusted fatal markers, invalid hidden evidence, simulator/container failure, or cleanup failure set `retry_eligible=true`.
- v1 remains on report schema 1 with `container_evidence`. v2 uses report schema 2 with distinct `candidate_container_evidence` and `sim_container_evidence`.
- Preserve score weights `15/15/20/25/10/15`, nine fixed worlds, ten repeated worlds, all fixed strict-pass, and repeated strict-pass rate at least 90%.
- Formal Docker acceptance is native Linux only.

## Locked File Structure

### `instance`

```text
pyvisa_dut_validation_v2/
├── pyvisa_iab/
│   ├── __init__.py              # WRAPPER_CLASS export and public version
│   ├── protocol.py              # public framing/tagged-value codec and RpcClient
│   └── highlevel.py             # IABVisaLibrary low-level PyVISA backend
├── runtime/
│   ├── requirements.lock        # PyVISA and runtime dependencies only
│   └── wheelhouse/              # only wheels installed into candidate image
├── starter/solution.py          # explicit @iab reference skeleton
├── tests/test_iab_protocol.py
├── tests/test_iab_backend.py
├── tests/test_candidate_boundary.py
├── Dockerfile                   # candidate-only UID 10001 image
├── image.lock.yaml
├── instance.yaml
├── prompt.md
└── transport/protocol.md
```

The existing `service/` tree is retained until the evaluator migration tests
pass, then deleted from `instance` in Task 10. It must not appear in the final
Docker context or visible manifest.

### `evaluator`

```text
vendor/pyvisa-sim-iab/                         # exact hooked 0.7.1 fork
evaluators/pyvisa_dut_validation_v2/
├── __init__.py
├── evaluator.yaml
├── world_contract.py                         # exact hidden WorldSpec JSON schema
├── bench.py                                  # dynamic resource map and hooks
├── dut.py
├── journal.py                                # raw complete hash chain
├── protocol.py                               # trusted codec/schema mirror
├── broker.py                                 # low-level SimVisaLibrary RPC
├── service.py                                # signal-driven sim entrypoint
├── evidence.py                               # journal/summary verifier
├── projection.py                             # raw events -> v1 semantic evidence
├── reports.py                                # schema-2 world/aggregate wrappers
├── simulator.yaml                            # hidden device template
├── reference/solution.py                     # direct official PyVISA solution
└── tests/
instrument_benchmark_evaluator/
├── contracts.py                              # v1/v2 request dispatch
├── cli.py                                    # run and serve-sim subcommands
├── v2_run.py                                 # per-world/formal v2 runner
└── container/
    ├── contracts.py                          # safe multi-file candidate contexts
    ├── sim_runner.py                         # trusted sibling lifecycle
    └── sim_evidence.py                       # sim inspect/evidence model
```

### `instrument`

```text
configs/pyvisa_dut_validation_v2.yaml
src/instrument_benchmark/contracts.py          # schema-aware v1/v2 validation
src/instrument_benchmark/orchestrator.py       # manifest resolution/image ID request
src/instrument_benchmark/evaluator_runtime.py  # exact image ID forwarding evidence
container/evaluator.Dockerfile                 # install hooked sim fork after deps
tests/test_v2_contracts.py
tests/test_orchestrator.py
tests/test_evaluator_runtime.py
tests/integration/test_v2_dual_container_linux.py
scripts/validate_distributed_benchmark.py
```

---

### Task 1: Permit audited multi-file candidate image contexts

**Repository:** `evaluator`

**Files:**
- Modify: `instrument_benchmark_evaluator/container/contracts.py`
- Modify: `instrument_benchmark_evaluator/container/dockerfile.py`
- Modify: `tests/test_container_contracts.py`
- Modify: `tests/test_dockerfile_policy.py`
- Create: `tests/fixtures/instance/public_backend.py`
- Create: `tests/fixtures/instance/wheelhouse/public.whl`

**Interfaces:**
- Produces: `ContainerContract.context_files: Mapping[str, str]` accepting any non-hidden regular file declared by hash.
- Produces: `validate_dockerfile()` guarantee that every local `COPY`/`ADD` source is declared in `context_files` and no hidden-name component is present.
- Preserves: v1's two-file context remains accepted unchanged.

- [ ] **Step 1: Write failing contract tests**

Add tests that build a temporary manifest with exact hashes for
`Dockerfile`, `image.lock.yaml`, `pyvisa_iab/highlevel.py`, and
`runtime/wheelhouse/pyvisa.whl`, then assert `load_container_contract()`
accepts it. Add rejection cases for an undeclared file, a directory/symlink,
`../` escape, hash mismatch, and declared paths containing any of:

```python
FORBIDDEN_CONTEXT_PARTS = {
    ".git", "candidate", "evaluator", "oracle", "worlds",
    "simulator", "instrument_service", "pyvisa_sim", "solution.py",
}
```

Add Dockerfile-policy tests for:

```dockerfile
COPY pyvisa_iab /usr/local/lib/python3.11/site-packages/pyvisa_iab
COPY runtime/wheelhouse /opt/wheels
```

and reject `COPY undeclared.py /opt/`, `COPY service /opt/service`, remote
`ADD`, and glob sources.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
cd evaluator
python -m unittest tests.test_container_contracts tests.test_dockerfile_policy -v
```

Expected: the multi-file context test fails with
`context_files must contain Dockerfile and lock`.

- [ ] **Step 3: Implement exact context validation**

Replace the two-file equality check with a loop that resolves every declared
relative path through `_child_file`, verifies its SHA-256, and requires the
Dockerfile and lock entries as a subset. Add:

```python
FORBIDDEN_CONTEXT_PARTS = frozenset({
    ".git", "evaluator", "oracle", "worlds", "simulator",
    "instrument_service", "pyvisa_sim", "solution.py",
})

def _validate_context_relative(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ContainerContractError("context file path escapes instance")
    if {part.lower() for part in path.parts} & FORBIDDEN_CONTEXT_PARTS:
        raise ContainerContractError("context file contains hidden material")
    return path
```

In `dockerfile.py`, parse `COPY`/local `ADD` sources as literal paths, reject
wildcards and `--from`, and require each source to equal a declared file or be
a parent whose declared descendants are all safe. Keep the pinned `FROM` and
final `USER 10001:10001` checks unchanged.

- [ ] **Step 4: Run focused and v1 regression tests**

Run:

```bash
cd evaluator
python -m unittest \
  tests.test_container_contracts \
  tests.test_dockerfile_policy \
  tests.test_container_image -v
```

Expected: PASS; existing v1 fixtures still load and validate.

- [ ] **Step 5: Commit**

```bash
cd evaluator
git add instrument_benchmark_evaluator/container/contracts.py \
  instrument_benchmark_evaluator/container/dockerfile.py \
  tests/test_container_contracts.py tests/test_dockerfile_policy.py \
  tests/fixtures/instance
git commit -m "feat: allow audited candidate image contexts"
```

### Task 2: Build the public framed RPC client and `@iab` backend

**Repository:** `instance`

**Files:**
- Create: `pyvisa_dut_validation_v2/pyvisa_iab/__init__.py`
- Create: `pyvisa_dut_validation_v2/pyvisa_iab/protocol.py`
- Create: `pyvisa_dut_validation_v2/pyvisa_iab/highlevel.py`
- Create: `pyvisa_dut_validation_v2/tests/test_iab_protocol.py`
- Create: `pyvisa_dut_validation_v2/tests/test_iab_backend.py`
- Modify: `pyproject.toml`
- Modify mechanically: `uv.lock`

**Interfaces:**
- Produces: `pyvisa_iab.WRAPPER_CLASS = IABVisaLibrary`.
- Produces: `RpcClient.call(operation: str, args: Mapping[str, WireValue]) -> tuple[WireValue, int | None]`.
- Produces: the exact PyVISA-sim 0.7.1 backend signatures listed in Global Constraints.

- [ ] **Step 1: Write protocol tests against a socket pair**

Test canonical four-byte big-endian framing, 1 MiB rejection, partial reads,
truncated frames, invalid UTF-8/JSON, duplicate JSON keys, exact top-level keys,
wrong version/request ID, and tagged values. The accepted tagged union is:

```python
None | bool | int | finite float | str | bytes | tuple[WireValue, ...]
```

Use `{"type":"bytes","base64":"..."}` for bytes and
`{"type":"list","items":[...]}` for tuples. Reject dicts as values,
non-finite floats, booleans where integers are required, invalid base64, and
unknown tags.

- [ ] **Step 2: Write backend tests with a fake broker**

The fake broker must assert these calls and return shapes:

```python
open_default_resource_manager() -> (101, StatusCode.success)
list_resources(101, "?*::INSTR") -> ("TCPIP::scope::INSTR",)
open(101, name, AccessModes.no_lock, 0) -> (202, StatusCode.success)
write(202, b"*IDN?\n") -> (6, StatusCode.success)
read(202, 4096) -> (b"IAB,SCOPE,1,1\n", StatusCode.success)
get_attribute(202, ResourceAttribute.timeout_value) -> (2000, StatusCode.success)
set_attribute(202, ResourceAttribute.timeout_value, 5000) -> StatusCode.success
disable_event(202, EventType.service_request, EventMechanism.queue) -> None
discard_events(202, EventType.service_request, EventMechanism.queue) -> None
close(202) -> StatusCode.success
```

Also assert a remote VISA error raises `pyvisa.errors.VisaIOError`, a lost
socket raises `VisaIOError(StatusCode.error_system_error)`, local handles never
equal the opaque remote token, and unsupported base-class methods remain
unsupported.

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
cd instance
uv sync
uv run python -m unittest \
  pyvisa_dut_validation_v2.tests.test_iab_protocol \
  pyvisa_dut_validation_v2.tests.test_iab_backend -v
```

Expected: FAIL because `pyvisa_iab` does not exist.

- [ ] **Step 4: Implement the public package**

`__init__.py` must be exactly equivalent to:

```python
from .highlevel import IABVisaLibrary

__version__ = "1.0.0"
WRAPPER_CLASS = IABVisaLibrary
__all__ = ["IABVisaLibrary", "WRAPPER_CLASS", "__version__"]
```

`IABVisaLibrary.get_library_paths()` returns `(LibraryPath("iab"),)`;
`get_debug_info()` returns only `{"Version":"1.0.0","Protocol":"1"}`.
`_init()` reads `IAB_VISA_SOCKET`, connects lazily, and initializes a
monotonic local-handle counter plus `dict[int, str]` token map. Implement
methods with the exact signatures obtained from PyVISA 1.16.2:

```python
def open(self, session, resource_name, access_mode=AccessModes.no_lock,
         open_timeout=0): ...
def list_resources(self, session, query="?*::INSTR"): ...
def read(self, session, count): ...
def write(self, session, data): ...
```

and the remaining methods from the test table. Convert returned integer
statuses with `StatusCode(value)`. Do not implement any other VISA operation.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
cd instance
uv run python -m unittest \
  pyvisa_dut_validation_v2.tests.test_iab_protocol \
  pyvisa_dut_validation_v2.tests.test_iab_backend -v
```

Expected: PASS.

Commit:

```bash
git add pyproject.toml uv.lock pyvisa_dut_validation_v2/pyvisa_iab \
  pyvisa_dut_validation_v2/tests/test_iab_protocol.py \
  pyvisa_dut_validation_v2/tests/test_iab_backend.py
git commit -m "feat: add explicit PyVISA iab backend"
```

### Task 3: Migrate the trusted v2 simulator core into evaluator ownership

**Repository:** `evaluator`

**Files:**
- Create: `vendor/pyvisa-sim-iab/**` from the instance's locked fork
- Create: `evaluators/pyvisa_dut_validation_v2/__init__.py`
- Create: `evaluators/pyvisa_dut_validation_v2/dut.py`
- Create: `evaluators/pyvisa_dut_validation_v2/world_contract.py`
- Create: `evaluators/pyvisa_dut_validation_v2/bench.py`
- Create: `evaluators/pyvisa_dut_validation_v2/journal.py`
- Create: `evaluators/pyvisa_dut_validation_v2/simulator.yaml`
- Create: `evaluators/pyvisa_dut_validation_v2/tests/test_bench.py`
- Create: `evaluators/pyvisa_dut_validation_v2/tests/test_journal.py`
- Create: `evaluators/pyvisa_dut_validation_v2/tests/test_sim_hooks.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `BenchContext.from_world(definition: Path, spec: WorldSpec, journal: EventJournal) -> BenchContext`.
- Produces: `dump_world(spec: WorldSpec, path: Path)` and
  `load_world(path: Path) -> WorldSpec` with exact keys and atomic writes.
- Produces: `BenchContext.visalib: SimVisaLibrary`, `snapshot() -> WorldSnapshot`, `force_safe()`, and `safe`.
- Produces: thread-safe `EventJournal.append(kind: str, **fields) -> JournalEvent` and `export(path)`.

- [ ] **Step 1: Copy the locked fork and service core without deleting the instance copy**

Mechanically copy:

```bash
cd /path/to/workspace
mkdir -p evaluator/vendor evaluator/evaluators/pyvisa_dut_validation_v2/tests
cp -R instance/pyvisa_dut_validation_v2/service/vendor/pyvisa-sim-iab \
  evaluator/vendor/pyvisa-sim-iab
cp instance/pyvisa_dut_validation_v2/service/instrument_service/dut.py \
  evaluator/evaluators/pyvisa_dut_validation_v2/dut.py
cp instance/pyvisa_dut_validation_v2/service/instrument_service/bench.py \
  evaluator/evaluators/pyvisa_dut_validation_v2/bench.py
cp instance/pyvisa_dut_validation_v2/service/instrument_service/journal.py \
  evaluator/evaluators/pyvisa_dut_validation_v2/journal.py
cp instance/pyvisa_dut_validation_v2/service/simulator.yaml \
  evaluator/evaluators/pyvisa_dut_validation_v2/simulator.yaml
```

Copying is only the migration start; the following tests require refactoring
before commit.

- [ ] **Step 2: Write hidden-world behavior tests**

Use v1 `WorldSpec.nominal()` plus every fixed-world override to assert:

- the `resource_map` controls names and order;
- distractors answer only their public identity;
- decimal/scientific DMM formats and 1/2/3 binary length digits are honored;
- dirty initial PSU/AWG/routes are applied;
- one-shot transient errors are deterministic;
- gain/offset/noise/settle/required routes come from the world;
- the existing four hooks reject atomically and record before/native/dynamic/
  after/error events;
- `snapshot()` returns the v1 `WorldSnapshot` shape and `force_safe()` clears
  outputs/routes.

Round-trip every `WorldSpec` through `world_contract.py`; reject missing/extra
keys, booleans in integer fields, non-finite floats, invalid roles/routes,
symlinks, and files larger than 64 KiB. Serialize sets/tuples as sorted JSON
arrays and write with temporary-file-plus-`os.replace` semantics.

Add a fork-diff test that compares the vendored fork to upstream 0.7.1 and
permits changes only in `pyvisa_sim/hooks.py` plus the approved hook call sites
and error-queue helpers in `devices.py`.

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
cd evaluator
PYTHONPATH=vendor/pyvisa-sim-iab:. python -m unittest \
  evaluators.pyvisa_dut_validation_v2.tests.test_bench \
  evaluators.pyvisa_dut_validation_v2.tests.test_journal \
  evaluators.pyvisa_dut_validation_v2.tests.test_sim_hooks -v
```

Expected: failures for fixed resource constants and missing world variations.

- [ ] **Step 4: Refactor the trusted core**

Instantiate `SimVisaLibrary(str(rendered_definition))` directly and store it as
`visalib`; do not create a high-level `ResourceManager` inside the sim. Render
the resource section of `simulator.yaml` into a private temporary file from
`WorldSpec.resource_map` and `distractors`. Replace fixed route/resource/DUT
values with fields from `WorldSpec`.

Every raw event must use:

```python
JournalEvent(
    run_id: str,
    world_id: str,
    sequence: int,
    monotonic_ns: int,
    previous_hash: str,
    kind: str,
    fields: dict[str, object],
    event_hash: str,
)
```

Protect append/export/final-hash with one `threading.RLock`. Record command
bytes/responses as base64 plus SHA-256 and retain complete state before/after.
Update `pyproject.toml` package data so the installed evaluator includes
`evaluators.pyvisa_dut_validation_v2` files `evaluator.yaml` and
`simulator.yaml`; Python modules and the vendored fork source remain tracked
build-context inputs.

- [ ] **Step 5: Run tests and commit**

Run the focused tests above, then:

```bash
git add vendor/pyvisa-sim-iab evaluators/pyvisa_dut_validation_v2 pyproject.toml
git commit -m "feat: move hidden v2 simulator into evaluator"
```

### Task 4: Implement the trusted low-level VISA broker and complete journal

**Repository:** `evaluator`

**Files:**
- Create: `evaluators/pyvisa_dut_validation_v2/protocol.py`
- Create: `evaluators/pyvisa_dut_validation_v2/broker.py`
- Create: `evaluators/pyvisa_dut_validation_v2/service.py`
- Create: `evaluators/pyvisa_dut_validation_v2/tests/test_protocol.py`
- Create: `evaluators/pyvisa_dut_validation_v2/tests/test_broker.py`
- Create: `evaluators/pyvisa_dut_validation_v2/tests/test_service.py`

**Interfaces:**
- Produces: `RemoteVisaBroker(bench, journal).serve_unix(endpoint, stop_event)`.
- Produces: `RemoteVisaBroker.freeze_and_close() -> BrokerSummary`.
- Produces: `service.main(["--world", ..., "--endpoint", ..., "--evidence", ...]) -> int`.

- [ ] **Step 1: Port the public protocol vectors into trusted tests**

Repeat every accepted/rejected vector from instance Task 2. Add a test that
serializes each request with the public encoder and parses it with the trusted
decoder, and the reverse for responses. Expected bytes must be literal fixtures
checked into both tests, not imported across repositories.

- [ ] **Step 2: Write broker behavior and adversarial tests**

Cover hello plus every allowed operation/return shape. Assert:

- random remote tokens map to real sim integer sessions;
- token ownership is connection-local and peer credentials are recorded;
- wrong UID, guessed/closed/cross-connection token, unknown operation, malformed
  frame, and over-limit connection are connection-local rejections;
- disconnect force-closes only that connection's sessions;
- `disable_event` and `discard_events` return `None`;
- any other VISA operation is rejected without reaching `SimVisaLibrary`;
- every request/result/status/reject produces one raw RPC event;
- broker internal invariant failure writes a trusted fatal event, while direct
  bad candidate traffic does not.

- [ ] **Step 3: Run tests and verify RED**

```bash
cd evaluator
PYTHONPATH=vendor/pyvisa-sim-iab:. python -m unittest \
  evaluators.pyvisa_dut_validation_v2.tests.test_protocol \
  evaluators.pyvisa_dut_validation_v2.tests.test_broker \
  evaluators.pyvisa_dut_validation_v2.tests.test_service -v
```

Expected: FAIL because protocol/broker/service modules do not exist.

- [ ] **Step 4: Implement dispatch and signal finalization**

Use an exact operation table rather than `getattr`:

```python
OPERATIONS = {
    "open_default_resource_manager",
    "list_resources",
    "open",
    "close",
    "read",
    "write",
    "get_attribute",
    "set_attribute",
    "disable_event",
    "discard_events",
}
```

Bind tokens to a `ConnectionState(connection_id, peer_uid, peer_gid,
peer_pid, sessions)` record. Wrap every `write` in
`bench.session_context(sha256(remote_token))`; the PyVISA-sim hook remains the
only SCPI recognition path. Catch `VisaIOError` as a normal VISA rejection;
catch only broker invariants as trusted fatal.

Create the socket with mode `0666` inside a mode `0755` transport directory.
The candidate's bind mount is read-only, so it can connect but cannot unlink or
replace the socket. Read Linux peer credentials with `SO_PEERCRED`; permit UID
10001 for hello/VISA operations and UID 11001 for readiness hello only.

`service.main()` installs SIGTERM/SIGINT handlers that set a stop event. After
accept stops, it freezes the broker, joins connection workers, records leaked
sessions, captures pre-cleanup snapshot, calls `force_safe()`, captures
post-cleanup snapshot, exports `events.jsonl`, and atomically writes
`summary.json`. On a trusted exception it atomically writes `fatal.json` and
returns 70. Do not add a socket finalize/admin operation.

- [ ] **Step 5: Run tests and commit**

```bash
git add evaluators/pyvisa_dut_validation_v2
git commit -m "feat: add remote PyVISA sim broker"
```

### Task 5: Make the v2 instance candidate-only and buildable offline

**Repository:** `instance`

**Files:**
- Modify: `pyvisa_dut_validation_v2/Dockerfile`
- Create: `pyvisa_dut_validation_v2/image.lock.yaml`
- Create: `pyvisa_dut_validation_v2/runtime/requirements.lock`
- Create: `pyvisa_dut_validation_v2/runtime/wheelhouse/pyvisa-1.16.2-py3-none-any.whl`
- Create: `pyvisa_dut_validation_v2/runtime/wheelhouse/typing_extensions-4.16.0-py3-none-any.whl`
- Modify: `pyvisa_dut_validation_v2/instance.yaml`
- Modify: `pyvisa_dut_validation_v2/prompt.md`
- Modify: `pyvisa_dut_validation_v2/starter/solution.py`
- Modify: `pyvisa_dut_validation_v2/transport/protocol.md`
- Replace: `pyvisa_dut_validation_v2/tests/test_container.py`
- Create: `pyvisa_dut_validation_v2/tests/test_candidate_boundary.py`
- Modify: `tests/test_instance.py`
- Modify: `.github/workflows/test.yml`

**Interfaces:**
- Produces: a locked UID `10001:10001` image with official PyVISA and
  `pyvisa_iab`, entrypoint `python /runner/bootstrap.py`, and no service/sim.
- Produces: formal v2 manifest compatible with evaluator Task 1.

- [ ] **Step 1: Write candidate-boundary tests**

Assert the final Docker context contains only declared public files, the
Dockerfile ends with `USER 10001:10001` and the evaluator bootstrap entrypoint,
and these paths/markers are absent from both context and final image:

```text
pyvisa_sim  instrument_service  simulator.yaml  hooks.py
BenchContext  DUTSpec  world  oracle  journal  /opt/service  /export
```

Run the image with `network=none`, import `pyvisa` and `pyvisa_iab`, assert
`import pyvisa_sim` fails, and assert `pyvisa.ResourceManager("@iab")` reaches a
fake host Unix socket mounted read-only.

- [ ] **Step 2: Run tests and verify RED**

```bash
cd instance
uv run python -m unittest tests.test_instance \
  pyvisa_dut_validation_v2.tests.test_container \
  pyvisa_dut_validation_v2.tests.test_candidate_boundary -v
```

Expected: failures because the current image runs a root supervisor and embeds
the trusted service.

- [ ] **Step 3: Build the candidate-only image contract**

Use the pinned Python base, install only the two candidate wheels with
`--no-index --require-hashes`, and copy `pyvisa_iab` into
`/usr/local/lib/python3.11/site-packages/pyvisa_iab` so the existing candidate
audit boundary treats it as ordinary runtime code. Do not add a public
`PYTHONPATH`. Finish with:

```dockerfile
WORKDIR /workspace
USER 10001:10001
ENTRYPOINT ["python", "/runner/bootstrap.py"]
```

The manifest must allow imports `pyvisa` and `pyvisa_iab`, forbid
`pyvisa_sim`, `qcodes`, and `pymeasure`, use gateway path
`/run/iab/visa.sock`, and declare hashes for every Docker context file.
Include the two `pyvisa_iab/*.py` files in `visible_files`; extend the instance
test's `VISIBLE_TOP_LEVEL` with `pyvisa_iab` so their public hashes are checked.
The prompt and starter must show:

```python
import pyvisa

def run_experiment(instrument_endpoint: str, output_path: str) -> dict:
    del instrument_endpoint
    resource_manager = pyvisa.ResourceManager("@iab")
    try:
        resources = resource_manager.list_resources()
        # The candidate implements discovery, experiment, result, and cleanup.
        raise NotImplementedError("complete the documented experiment")
    finally:
        resource_manager.close()
```

Do not set `PYVISA_LIBRARY`; runtime sets only
`IAB_VISA_SOCKET=/run/iab/visa.sock`.

- [ ] **Step 4: Generate hashes/digest and run tests**

Stage exactly the declared context files, build with BuildKit/network none,
inspect the image ID, then write that exact ID plus Dockerfile SHA-256 into
`image.lock.yaml` and refresh `instance.yaml` hashes. Run:

```bash
cd instance
uv run python -m unittest discover -s tests -v
IAB_RUN_DOCKER_TESTS=1 uv run python -m unittest \
  pyvisa_dut_validation_v2.tests.test_container \
  pyvisa_dut_validation_v2.tests.test_candidate_boundary -v
```

Expected: PASS and `docker image inspect` reports UID `10001:10001` with no
`pyvisa_sim` module.

- [ ] **Step 5: Commit**

```bash
git add pyvisa_dut_validation_v2 tests/test_instance.py .github/workflows/test.yml
git commit -m "feat: build candidate-only PyVISA v2 image"
```

### Task 6: Add trusted sim-container creation, readiness, finalization, and evidence

**Repository:** `evaluator`

**Files:**
- Create: `instrument_benchmark_evaluator/container/sim_evidence.py`
- Create: `instrument_benchmark_evaluator/container/sim_runner.py`
- Create: `tests/test_sim_container_runner.py`
- Create: `tests/test_sim_evidence.py`
- Modify: `instrument_benchmark_evaluator/container/docker_client.py`
- Modify: `instrument_benchmark_evaluator/cli.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `SimContainerRunner.start(...) -> SimContainerHandle`.
- Produces: `SimContainerRunner.finalize(handle) -> SimContainerResult`.
- Produces: `SimContainerResult(container_evidence, journal_evidence, fatal)`.

- [ ] **Step 1: Write exact Docker argument/lifecycle tests**

With a fake `DockerClient`, assert `start()` creates the sim from the injected
`sha256:...` image ID with `--network=none`, read-only root, UID 11001,
cap-drop/all, no-new-privileges, bounded resources, labels, RW transport/evidence
mounts, RO single-file world mount, no Docker socket, and command:

```text
serve-sim --world /run/iab/world.json
          --endpoint /run/iab/transport/visa.sock
          --evidence /run/iab/evidence
```

Assert readiness uses host-side hello on the socket. Assert `finalize()` sends
SIGTERM, waits, inspects before removal, verifies evidence, then removes. Add
failure tests for bind timeout, early exit, non-zero exit, OOM, missing summary,
bad hash, unsafe post-state, SIGTERM timeout, inspect failure, and remove failure.

- [ ] **Step 2: Run tests and verify RED**

```bash
cd evaluator
python -m unittest tests.test_sim_container_runner tests.test_sim_evidence -v
```

Expected: import failure for the new modules.

- [ ] **Step 3: Implement the lifecycle and verifier**

Extend `DockerClient` with exact argv-based `start_detached`, `signal`, and
`wait` methods. `sim_evidence.verify_evidence()` must bound file sizes, reject
symlinks/non-regular files, parse every JSONL record, verify sequence starts at
1 with no gaps, previous-hash linkage, recomputed event hash, summary count,
terminal hash, required lifecycle kinds, pre/post snapshots, and `safe=true`.

Sim evidence records normalized inspect data plus empty stdout/stderr hashes
(the sim runs with `--log-driver=none` and writes diagnostics to private
evidence). A candidate-caused connection rejection never creates `fatal.json`.

- [ ] **Step 4: Add `serve-sim` CLI dispatch and run tests**

`instrument_benchmark_evaluator.cli` must register a `serve-sim` subcommand
that calls the trusted service with absolute paths and returns its exact exit
code. Run focused tests plus existing CLI tests; expected PASS.

- [ ] **Step 5: Commit**

```bash
git add instrument_benchmark_evaluator/container/sim_evidence.py \
  instrument_benchmark_evaluator/container/sim_runner.py \
  instrument_benchmark_evaluator/container/docker_client.py \
  instrument_benchmark_evaluator/cli.py pyproject.toml \
  tests/test_sim_container_runner.py tests/test_sim_evidence.py
git commit -m "feat: manage trusted sim sibling containers"
```

### Task 7: Project complete v2 events into the existing oracle and score

**Repository:** `evaluator`

**Files:**
- Create: `evaluators/pyvisa_dut_validation_v2/projection.py`
- Create: `evaluators/pyvisa_dut_validation_v2/reports.py`
- Create: `evaluators/pyvisa_dut_validation_v2/tests/test_projection.py`
- Create: `evaluators/pyvisa_dut_validation_v2/tests/test_scoring.py`
- Modify: `evaluators/pyvisa_dut_validation_v1/gateway/journal.py`
- Modify: `evaluators/pyvisa_dut_validation_v1/scoring.py`

**Interfaces:**
- Produces: `project_events(raw: Sequence[JournalEvent]) -> tuple[EvidenceEvent, ...]`.
- Produces: `V2WorldReport` and `V2EvaluationReport.to_dict()` with schema 2.
- Preserves: v1 serialization byte-for-byte except optional internal dataclass defaults not emitted.

- [ ] **Step 1: Write projection/scoring tests**

Build raw fixtures for open, attribute changes, writes/queries, responses,
explicit close, forced close, protocol reject, and force-safe. Assert projected
events drive the existing oracle and constraints to the same semantic result as
v1 nominal evidence. Every projected event must contain `source_sequence`.

Because PyVISA keeps `write_termination` locally, infer its correct use only
from actual command bytes ending in the simulator's required terminator. Emit a
projected driver-configuration event referencing that raw write; do not add an
RPC/backend method. Project timeout/read termination only from real
`set_attribute` RPC events.

Assert v2 reports rename the v1 candidate evidence to
`candidate_container_evidence`, add `sim_container_evidence` and
`sim_journal_evidence`, use schema version 2, and allow missing container
evidence only when `infrastructure_valid=false` and `retry_eligible=true`.

- [ ] **Step 2: Run tests and verify RED**

```bash
cd evaluator
PYTHONPATH=vendor/pyvisa-sim-iab:. python -m unittest \
  evaluators.pyvisa_dut_validation_v2.tests.test_projection \
  evaluators.pyvisa_dut_validation_v2.tests.test_scoring -v
```

- [ ] **Step 3: Implement a pure projection**

Add `source_sequence: int | None = None` to internal `EvidenceEvent` without
adding it to v1 `to_dict()` output. Generate projection sequence numbers as
`raw.sequence * 10 + offset` so inferred configuration and command evidence are
stable and ordered. Preserve exact command/response bytes from hidden hook
events, map nested v2 state to `WorldSnapshot` fields, and mark candidate versus
forced cleanup distinctly.

Wrap, rather than modify, base v1 reports:

```python
@dataclass(frozen=True)
class V2WorldReport:
    base: WorldReport
    candidate_container_evidence: dict[str, Any] | None
    sim_container_evidence: dict[str, Any] | None
    sim_journal_evidence: dict[str, Any] | None

@dataclass(frozen=True)
class V2EvaluationReport:
    base: EvaluationReport
    worlds: tuple[V2WorldReport, ...]
```

- [ ] **Step 4: Run v2 and complete v1 regression suites**

```bash
python -m unittest \
  evaluators.pyvisa_dut_validation_v2.tests.test_projection \
  evaluators.pyvisa_dut_validation_v2.tests.test_scoring -v
python -m unittest discover -s evaluators/pyvisa_dut_validation_v1/tests -v
```

Expected: all PASS; a captured v1 report fixture remains equal.

- [ ] **Step 5: Commit**

```bash
git add evaluators/pyvisa_dut_validation_v1 \
  evaluators/pyvisa_dut_validation_v2/projection.py \
  evaluators/pyvisa_dut_validation_v2/reports.py \
  evaluators/pyvisa_dut_validation_v2/tests
git commit -m "feat: score complete v2 simulator evidence"
```

### Task 8: Run one dual-container world and the formal nineteen-world suite

**Repository:** `evaluator`

**Files:**
- Create: `instrument_benchmark_evaluator/v2_run.py`
- Create: `tests/test_v2_run.py`
- Create: `evaluators/pyvisa_dut_validation_v2/evaluator.yaml`
- Create: `evaluators/pyvisa_dut_validation_v2/reference/solution.py`
- Create: `evaluators/pyvisa_dut_validation_v2/negatives/bad_protocol.py`
- Create: `evaluators/pyvisa_dut_validation_v2/negatives/leaked_sessions.py`
- Create: `evaluators/pyvisa_dut_validation_v2/tests/test_end_to_end.py`
- Modify: `instrument_benchmark_evaluator/contracts.py`
- Modify: `instrument_benchmark_evaluator/cli.py`
- Modify: `instrument_benchmark_evaluator/candidate_backend.py`
- Modify: `instrument_benchmark_evaluator/container/runner.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `run_v2_world(...) -> V2WorldExecution` and
  `run_v2_full_suite(...) -> V2EvaluationReport`.
- Consumes: exact `evaluator_image_id` from the v2 request.

- [ ] **Step 1: Write lifecycle/order/outcome tests**

Fake both runners and assert this exact order:

```text
create dirs/world -> start sim -> hello -> invoke candidate
-> inspect/remove candidate -> SIGTERM sim -> inspect/verify/remove sim
-> project/oracle/grade
```

The `create dirs/world` transition must call Task 3's
`dump_world(spec, world_root / "world.json")`, chmod the world file read-only,
and create separate `transport`, `evidence`, `workspace`, and `output`
directories under the per-world temporary root.

Test completed, candidate failure, timeout, OOM, output limit, invalid result,
bad raw socket traffic, leaked sessions, sim readiness failure, sim early exit,
bad journal, unsafe finalization, and cleanup failure. Candidate outcomes are
non-retryable when sim evidence validates; trusted sim/container failures are
retryable and produce a schema-2 world report rather than a candidate score.

- [ ] **Step 2: Write the reference candidate with direct official PyVISA**

Port the v1 reference logic, but import only `pyvisa` and standard library and
construct exactly:

```python
resource_manager = pyvisa.ResourceManager("@iab")
```

Discover all roles through `*IDN?`, set timeout/read/write termination on each
message resource, perform the documented waveform/DMM/scope experiment, use
PyVISA ASCII/binary helpers, retry the declared transient instrument error,
actively disable outputs/open routes/close every resource, write `result.json`,
and close the resource manager in `finally`.

- [ ] **Step 3: Run tests and verify RED**

```bash
cd evaluator
PYTHONPATH=vendor/pyvisa-sim-iab:. python -m unittest \
  tests.test_v2_run \
  evaluators.pyvisa_dut_validation_v2.tests.test_end_to_end -v
```

- [ ] **Step 4: Implement v2 request/CLI dispatch and suite**

Allow both evaluator IDs. v1 requests retain their exact field set. v2 adds one
required field:

```json
"evaluator_image_id": "sha256:<64 lowercase hex>"
```

`load_instance_settings()` takes the expected evaluator ID. `cli.run` selects
v1 or v2 from `instance_id`, loads the matching manifest, and never sends the
v2 image ID through candidate environment. Candidate runner adds only:

```text
--env=IAB_VISA_SOCKET=/run/iab/visa.sock
```

and continues to mount the transport parent read-only.

- [ ] **Step 5: Run all evaluator unit tests and commit**

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s evaluators/pyvisa_dut_validation_v1/tests -v
PYTHONPATH=vendor/pyvisa-sim-iab:. python -m unittest discover \
  -s evaluators/pyvisa_dut_validation_v2/tests -v
git add instrument_benchmark_evaluator evaluators/pyvisa_dut_validation_v2 pyproject.toml tests
git commit -m "feat: run formal v2 dual-container suites"
```

### Task 9: Install the hooked simulator in the trusted evaluator image

**Repository:** `instrument`

**Files:**
- Modify: `container/evaluator.Dockerfile`
- Modify: `tests/test_evaluator_image.py`
- Modify: `tests/integration/test_evaluator_image_linux.py`

**Interfaces:**
- Produces: evaluator image where `pyvisa_sim.__version__` and hook symbols come
  from `vendor/pyvisa-sim-iab`, while the image remains UID 11001 and offline.

- [ ] **Step 1: Write image-context and installed-fork tests**

Assert the build context contains the tracked vendor source and the Dockerfile
installs it after `evaluator-requirements.lock` with:

```dockerfile
RUN python -m pip install --no-index --no-deps --no-build-isolation \
      /build/evaluator/vendor/pyvisa-sim-iab \
 && python -m pip install --no-index --no-deps --no-build-isolation \
      /build/evaluator
```

The Linux image test must import `pyvisa_sim.hooks.CommandContext`, assert the
version is `0.7.1+iab1`, and run the fork-diff test inside the image.

- [ ] **Step 2: Run tests and verify RED**

```bash
cd instrument
python -m unittest tests.test_evaluator_image -v
```

- [ ] **Step 3: Modify the Dockerfile and run unit tests**

Keep locked offline wheels, Docker CLI verification, UID 11001, and read-only
runtime assumptions unchanged. Run `tests.test_evaluator_image`; expected PASS.

- [ ] **Step 4: Run the native-Linux image test when available**

```bash
IAB_RUN_DOCKER_TESTS=1 python -m unittest \
  tests.integration.test_evaluator_image_linux -v
```

Expected on Linux Docker: PASS. On non-Linux: documented skip.

- [ ] **Step 5: Commit**

```bash
git add container/evaluator.Dockerfile tests/test_evaluator_image.py \
  tests/integration/test_evaluator_image_linux.py
git commit -m "build: install hooked PyVISA sim in evaluator image"
```

### Task 10: Add v2 orchestration, schemas, provenance, and report validation

**Repository:** `instrument`

**Files:**
- Create: `configs/pyvisa_dut_validation_v2.yaml`
- Create: `tests/test_v2_contracts.py`
- Modify: `src/instrument_benchmark/contracts.py`
- Modify: `src/instrument_benchmark/orchestrator.py`
- Modify: `src/instrument_benchmark/evaluator_runtime.py`
- Modify: `tests/test_orchestrator.py`
- Modify: `tests/test_evaluator_runtime.py`
- Modify: `schemas/run.schema.json`

**Interfaces:**
- Produces: evaluator manifest resolver with v1 root fallback and v2 package
  manifest lookup.
- Produces: schema-dispatched `validate_evaluator_report(value, evaluator_id)`.
- Produces: v2 request containing exact outer evaluator image ID.

- [ ] **Step 1: Write v2 contract/report tests**

Assert v1 fixtures still validate with schema 1 and `container_evidence`.
Assert v2 schema 2 requires both sibling evidence dictionaries for valid
infrastructure, verifies candidate UID 10001 and sim UID 11001, verifies both
`network_mode=none`, read-only roots, mount allowlists, cleanup success, journal
terminal hash/count, and report evaluator ID. For declared infrastructure
failure, allow a missing sibling evidence object only with
`infrastructure_valid=false`, `retry_eligible=true`, and a non-empty trusted
error list.

- [ ] **Step 2: Write orchestration request tests**

The fake builder returns image ID `sha256:` plus 64 hex. Assert the v2 request
contains that exact value, while a v1 request does not gain an extra field.
Assert the resolver uses:

```text
evaluator/evaluators/pyvisa_dut_validation_v2/evaluator.yaml
```

for v2 and existing `evaluator/evaluator.yaml` for v1.

- [ ] **Step 3: Run tests and verify RED**

```bash
cd instrument
python -m unittest tests.test_v2_contracts tests.test_orchestrator \
  tests.test_evaluator_runtime -v
```

- [ ] **Step 4: Implement schema dispatch and config**

Keep `RunConfig` schema version 1; evaluator/report versions are declared by the
selected evaluator manifest. Add helper:

```python
def evaluator_manifest_path(checkout: Path, evaluator_id: str) -> Path:
    packaged = checkout / "evaluators" / evaluator_id / "evaluator.yaml"
    legacy = checkout / "evaluator.yaml"
    return packaged if packaged.is_file() else legacy
```

Pass `evaluator_image.image_id` into v2 request JSON only. Record evaluator
image ID/digest in orchestration and continue recording candidate image lock,
Docker Engine version, and all three repository commits.

- [ ] **Step 5: Run unit regression and commit**

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
git add configs/pyvisa_dut_validation_v2.yaml schemas/run.schema.json \
  src/instrument_benchmark tests
git commit -m "feat: orchestrate formal PyVISA v2 runs"
```

### Task 11: Remove hidden v2 service material from the instance repository

**Repository:** `instance`

**Files:**
- Delete: `pyvisa_dut_validation_v2/service/**`
- Delete: `pyvisa_dut_validation_v2/starter/instrument_client.py`
- Delete/replace: old service/bench/client/journal tests under
  `pyvisa_dut_validation_v2/tests/`
- Modify: `pyvisa_dut_validation_v2/instance.yaml`
- Modify: `README.md`
- Modify: `pyvisa_dut_validation_v2/ACCEPTANCE.md`

**Interfaces:**
- Produces: no hidden simulator/service implementation anywhere in the formal
  instance v2 tree.
- Preserves: v1 and public v2 backend/image tests.

- [ ] **Step 1: Add a repository-wide secrecy test**

Walk every tracked file under `pyvisa_dut_validation_v2` except historical
design documents and assert none of these packages/files remain:

```text
service/  instrument_service  pyvisa_sim/  simulator.yaml
bench.py  dut.py  journal.jsonl  summary.json
```

Also assert visible material contains no hidden world values or expected
answers.

- [ ] **Step 2: Verify RED, then delete migrated files**

Run the secrecy test; expected failure on `service/`. Remove only the migrated
v2 proof-of-concept service/client/tests. Do not touch
`pyvisa_dut_validation_v1`.

- [ ] **Step 3: Refresh exact manifest hashes**

Recompute `visible_files`, `container.context_files`, Dockerfile SHA-256, and
the locked candidate image digest after deletion. Verify `instance.yaml` names
only public files and `image.lock.yaml`.

- [ ] **Step 4: Run the complete instance suite**

```bash
cd instance
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s pyvisa_dut_validation_v2/tests -v
```

Expected: PASS with no single-container supervisor/service test remaining.

- [ ] **Step 5: Commit**

```bash
git add -A pyvisa_dut_validation_v2 README.md
git commit -m "refactor: remove hidden simulator from v2 instance"
```

### Task 12: Native-Linux dual-container security and formal scoring gate

**Repositories:** `evaluator`, then `instrument`, then `instance` CI metadata

**Files:**
- Create: `evaluator/tests/integration/test_v2_dual_container_linux.py`
- Modify: `evaluator/.github/workflows/test.yml`
- Create: `instrument/tests/integration/test_v2_dual_container_linux.py`
- Modify: `instrument/scripts/validate_distributed_benchmark.py`
- Modify: `instrument/.github/workflows/distributed-docker.yml`
- Modify: `instance/.github/workflows/test.yml`
- Modify: `evaluator/README.md`
- Modify: `instrument/README.md`
- Modify: `instance/README.md`

**Interfaces:**
- Produces: one formal v2 validation report with nineteen worlds, three runtime
  container roles, complete journal evidence, score 100, and strict pass for the
  reference candidate.

- [ ] **Step 1: Write evaluator native-Linux isolation tests**

With `IAB_RUN_DOCKER_TESTS=1`, run nominal/reference and adversarial candidates.
Inspect both siblings and assert exact image IDs, users, security settings,
mount allowlists, no networks, no Docker sockets, candidate inability to read
world/evidence/unlink socket, sim inability to read candidate, unique container
IDs per world, candidate-first cleanup, safe final state, and valid journal
hash chain. Assert bad protocol and leaked sessions are non-retryable.

- [ ] **Step 2: Write formal instrument integration test**

Run `configs/pyvisa_dut_validation_v2.yaml` through the real outer evaluator
container. Assert 9 fixed + 10 repeated reports, score 100, strict pass,
`infrastructure_valid=true`, `retry_eligible=false`, three clean repository
provenance entries, outer evidence, and distinct candidate/sim evidence on every
world. Assert the reference file contains literal
`pyvisa.ResourceManager("@iab")`.

- [ ] **Step 3: Run all non-Docker suites first**

```bash
cd instance && uv run python -m unittest discover -s tests -v
cd ../evaluator && python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s evaluators/pyvisa_dut_validation_v1/tests -v
PYTHONPATH=vendor/pyvisa-sim-iab:. python -m unittest discover \
  -s evaluators/pyvisa_dut_validation_v2/tests -v
cd ../instrument && python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: PASS except explicit native-Linux Docker skips.

- [ ] **Step 4: Run native-Linux Docker and formal suites**

```bash
cd evaluator
IAB_RUN_DOCKER_TESTS=1 python -m unittest \
  tests.integration.test_v2_dual_container_linux -v
cd ../instrument
IAB_RUN_DOCKER_TESTS=1 python -m unittest \
  tests.integration.test_v2_dual_container_linux -v
PYTHONPATH=src python scripts/validate_distributed_benchmark.py \
  --config configs/pyvisa_dut_validation_v2.yaml
```

Expected: reference strict pass over all nineteen worlds; every isolation and
evidence assertion passes; no run-owned containers remain.

- [ ] **Step 5: Update CI/docs and commit per repository**

Add v2 unit jobs and native-Linux v2 Docker jobs without removing v1 jobs.
Cleanup remains label-scoped. Document explicit `@iab`, two workload siblings,
outer Docker authority, networkless Unix socket, capability ceiling, retry
classification, and native-Linux requirement.

Commit evaluator:

```bash
git add tests/integration/test_v2_dual_container_linux.py .github/workflows/test.yml README.md
git commit -m "test: verify v2 dual-container isolation"
```

Commit instrument:

```bash
git add tests/integration/test_v2_dual_container_linux.py \
  scripts/validate_distributed_benchmark.py \
  .github/workflows/distributed-docker.yml README.md
git commit -m "test: validate formal PyVISA v2 chain"
```

Commit instance if CI/README changed after Task 11:

```bash
git add .github/workflows/test.yml README.md
git commit -m "ci: build and test candidate-only v2 image"
```

## Final Verification

- [ ] **Verify all three worktrees are clean and commits are repository-local**

```bash
git -C instance status --short
git -C evaluator status --short
git -C instrument status --short
```

Expected: no output from any command.

- [ ] **Verify forbidden implementation material is absent from candidate image**

```bash
docker run --rm --network none --entrypoint python \
  iab/pyvisa-dut-validation:v2 -I -c \
  'import importlib.util,pyvisa,pyvisa_iab; assert importlib.util.find_spec("pyvisa_sim") is None'
```

Expected: exit 0.

- [ ] **Verify no runtime containers leaked**

```bash
docker ps -a --filter label=iab.managed=true --filter label=iab.run=<final-run-id>
```

Expected: header only.

- [ ] **Verify the final report's formal invariants**

The final v2 report must have schema 2, score 100, strict pass, nine fixed and
ten repeated worlds, `infrastructure_valid=true`, `retry_eligible=false`, valid
outer evaluator evidence, and valid candidate/sim evidence plus complete linked
journal evidence for each world. Re-run the instrument report validator against
the persisted report; expected PASS.
