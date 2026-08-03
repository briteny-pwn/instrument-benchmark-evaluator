# PyVISA v2 Dual-Container Remote Backend Design

Date: 2026-08-03

Status: approved design; implementation pending

## Objective

Promote `pyvisa_dut_validation_v2` from its current single-container proof of
concept into the formal three-repository evaluation chain while preserving a
strict boundary between candidate-visible code and the hidden simulation.

For every world, the untrusted candidate runs in one container and the trusted
PyVISA-sim service runs in a separate sibling container. Both containers have
networking disabled and communicate only through a run-scoped Unix-domain
socket. The candidate uses the official PyVISA frontend and must select the
public remote backend explicitly:

```python
import pyvisa

resource_manager = pyvisa.ResourceManager("@iab")
```

The remote backend exposes only the low-level operations already implemented
by the pinned PyVISA-sim backend. It does not add event, asynchronous, trigger,
locking, bus-control, or other VISA features that PyVISA-sim does not provide.

v1 and v2 remain separate, supported benchmark contracts. This design does not
replace or silently change v1.

## Decision Summary

- Use a low-level VISA RPC boundary, not the proof-of-concept's high-level
  `InstrumentClient` protocol.
- Package a small public `pyvisa_iab` backend in the v2 candidate image.
- Require candidates to call `pyvisa.ResourceManager("@iab")` explicitly.
- Keep PyVISA's high-level `ResourceManager` and resource classes in the
  candidate container.
- Keep the actual `SimVisaLibrary`, hooked PyVISA-sim, device definitions,
  `BenchContext`, dynamic DUT, hidden worlds, journal, oracle, and scoring in
  the evaluator image.
- Run one fresh sim sibling and one fresh candidate sibling for every fixed or
  repeated world.
- Give neither sibling a network namespace with connectivity or access to the
  Docker socket.
- Share only one socket transport directory between the siblings. The sim sees
  it read-write; the candidate sees it read-only.
- Let the trusted outer evaluator own startup, readiness, termination,
  finalization, evidence collection, scoring, and removal.
- Preserve every accepted/rejected RPC, SCPI command, state transition,
  cleanup, and lifecycle event in the hidden hash-chained journal. Redaction
  removes secret values, not event records.
- Reuse the current nine fixed and ten repeated world schedule, the six score
  weights, oracle semantics, strict gates, and retry policy through a v2
  adapter. Do not alter v1 report semantics.

## Alternatives Considered

### A. Low-level VISA RPC — selected

`IABVisaLibrary` forwards the small subset of `VisaLibraryBase` calls provided
by `SimVisaLibrary` over the Unix socket. The trusted broker invokes the real
PyVISA-sim backend and returns exact result bytes, counts, attribute values, and
VISA status codes.

This preserves the normal PyVISA frontend, resource classes, parsing helpers,
and status handling while moving all simulator state and hidden behavior out of
the candidate container.

### B. Forward the proof-of-concept high-level client operations — rejected

The current v2 service exposes operations such as `query_text`, `query_bytes`,
and configuration setters. Wrapping these behind a PyVISA backend would make
the private service, rather than PyVISA, define query composition, buffering,
termination, chunking, and error behavior. That would create observable
semantic drift from normal PyVISA.

### C. Keep VISA sessions in the candidate and send only SCPI commands — rejected

This places buffering, pending responses, attributes, and part of the session
state on the untrusted side. It weakens the trust boundary and makes the
candidate-visible backend responsible for simulator semantics.

## Repository Ownership

The authoritative implementation remains split across the existing three
repositories.

### `instrument`

`instrument` owns orchestration and cross-repository validation:

- a new `pyvisa_dut_validation_v2` run configuration;
- v2-specific instance/evaluator/report schema dispatch;
- instance and evaluator checkout provenance;
- exact candidate and evaluator image provenance;
- creation of the trusted outer evaluator container;
- the host run root mounted at the identical absolute path into the outer
  evaluator;
- validation of outer, sim, and candidate container evidence;
- final report persistence and retry eligibility.

The existing v1 configuration and v1 validation path remain valid without a
schema migration.

### `instance`

`instance/pyvisa_dut_validation_v2` becomes candidate-only material:

- prompt and controlled instrument manuals;
- starter solution and result schema;
- official PyVISA 1.16.2;
- public `pyvisa_iab` backend;
- pinned offline wheels and candidate Dockerfile/lock metadata.

The formal v2 candidate image must not contain PyVISA-sim, the hooked fork,
device YAML, service implementation, `BenchContext`, DUT specification, hidden
worlds, oracle, scoring, journal, or expected results. The old single-container
proof-of-concept service is migrated to the evaluator and removed from the
candidate build context. Git history remains the record of the proof of
concept.

The v2 manifest changes `allowed_import_roots` to permit `pyvisa` and
`pyvisa_iab`. It continues to forbid `pyvisa_sim`, QCoDeS, PyMeasure, and hidden
evaluator modules. Import restrictions are defense in depth; absence from the
candidate image is the primary secrecy control.

### `evaluator`

`evaluator` owns a separate `pyvisa_dut_validation_v2` evaluator ID and all
trusted v2 behavior:

- `RemoteVisaBroker` and wire protocol;
- hooked, pinned PyVISA-sim 0.7.1;
- simulator YAML and five device models;
- `BenchContext`, dynamic DUT, virtual/monotonic time, and error queues;
- fixed and repeated world definitions;
- complete event journal and final summary;
- v2 evidence normalization;
- oracle, partial-order constraints, score aggregation, and strict gates;
- sim-container entrypoint and finalization signal handling.

The evaluator Docker image is both the outer evaluator image and the sim
sibling image. The outer evaluator starts the sim by exact image ID/digest with
an overridden command. No fourth repository or separately distributed hidden
image is introduced.

## Runtime Topology

The formal run has a trusted outer evaluator container plus two per-world
sibling containers:

```text
Docker host
|
`-- trusted outer evaluator container (UID 11001, network=none)
    |   Docker socket mounted; exact evaluator image ID injected
    |   host run root mounted at the same absolute path
    |
    |-- creates trusted sim sibling (UID 11001, network=none)
    |   |-- evaluator image by exact ID
    |   |-- hidden world and simulator code in read-only image
    |   |-- transport/ mounted read-write
    |   `-- evidence/ mounted read-write
    |
    `-- creates untrusted candidate sibling (UID 10001, network=none)
        |-- locked v2 instance image
        |-- candidate bundle mounted read-only
        |-- transport/ mounted read-only
        `-- /output and /tmp on bounded writable mounts
```

The sim and candidate are the two workload containers. The outer evaluator is
not merged into either workload and is the only runtime component with Docker
authority.

All three runtime containers use `network=none`. The sim and candidate have no
Docker socket, device mounts, host namespaces, or evaluator source mounts.

## Per-World Filesystem Layout

The outer evaluator creates a fresh private root for each world:

```text
<host-run-root>/<run-token>/<world-token>/
|-- transport/
|   `-- visa.sock
|-- evidence/
|   |-- events.jsonl
|   |-- summary.json
|   `-- fatal.json                 # only when a trusted fatal occurs
`-- world.json
```

Mount policy:

| Path | Outer evaluator | Sim sibling | Candidate sibling |
|---|---|---|---|
| `transport/` | read/write | read/write | read-only |
| `evidence/` | read after sim stop | read/write | not mounted |
| `world.json` | creates | read-only | not mounted |
| candidate workspace | stages | not mounted | read-only |
| `/output` | collects after stop | not mounted | bounded writable |

The socket parent is not candidate-writable. The socket grants connect access
to UID 10001, but the read-only bind prevents unlinking or replacement. The
directory contains no secret metadata.

Linux `SO_PEERCRED` identifies and journals the peer UID, GID, and PID. UID
10001 may use public handshake and VISA operations. UID 11001 may perform the
same non-mutating readiness handshake. There is no protocol-level finalize,
shutdown, evidence, world, snapshot, or administrative operation.

Native Linux Docker Engine remains the supported target. Docker Desktop path
and Unix-socket behavior are outside the formal acceptance target.

## Candidate API and Backend Discovery

The required public use is:

```python
import pyvisa

rm = pyvisa.ResourceManager("@iab")
resources = rm.list_resources()
scope = rm.open_resource(resources[0])
identity = scope.query("*IDN?")
```

PyVISA resolves `@iab` by importing `pyvisa_iab` and reading:

```python
WRAPPER_CLASS = IABVisaLibrary
```

`IABVisaLibrary` subclasses `pyvisa.highlevel.VisaLibraryBase`. It contains no
SCPI knowledge, resource names, hidden rules, DUT parameters, device-specific
methods, oracle logic, or fallback simulator. Its backend-library argument is
empty for `@iab`; it reads only the fixed socket endpoint and protocol settings
injected by the candidate bootstrap.

The backend maintains a process-local mapping:

```text
PyVISA local integer session -> broker opaque remote token
```

Tokens are never interpreted locally. A persistent socket and request lock
serialize backend calls. Multiple PyVISA resource objects may share the
backend connection, as they do with PyVISA's backend singleton behavior.

Candidates may open the Unix socket directly because it is their transport.
That grants no additional operations or evidence access. Malformed or
unauthorized protocol use is handled as candidate-originated traffic, not as a
trusted infrastructure failure.

## PyVISA and PyVISA-sim Responsibility Boundary

The official PyVISA frontend in the candidate continues to provide:

- `ResourceManager` and resource class selection;
- `MessageBasedResource.query()` composition;
- text, ASCII, and binary parsing/formatting helpers;
- chunked reads and termination configuration;
- timeout and resource attribute wrappers;
- context management and normal exception/status translation.

The remote path is:

```text
candidate Resource method
-> official PyVISA high-level code
-> IABVisaLibrary low-level method
-> framed Unix-socket RPC
-> RemoteVisaBroker
-> real SimVisaLibrary low-level method
-> PyVISA-sim session and Device.write/read
-> native dialogue/property/register/error matching
-> existing benchmark hooks and dynamic DUT
-> exact low-level result and VISA status
```

The broker forwards only the operations implemented by the original pinned
PyVISA-sim backend:

| Operation | Result |
|---|---|
| `open_default_resource_manager` | remote resource-manager token and status |
| `list_resources` | tuple of resource strings (the original sim method has no status return) |
| `open` | remote resource token and status; called by PyVISA's frontend `open_bare_resource()` |
| `close` | status |
| `read` | exact bytes and status |
| `write` | accepted byte count and status |
| `get_attribute` | typed attribute value and status |
| `set_attribute` | status |
| `disable_event` | original PyVISA-sim no-op behavior |
| `discard_events` | original PyVISA-sim no-op behavior |

Backend base-class methods not implemented by PyVISA-sim remain unsupported.
In particular, v2 does not add enable/wait event operations, event handlers,
SRQ, asynchronous I/O, trigger, lock, memory, or GPIB bus-control support.

The existing four PyVISA-sim hooks remain because they implement hidden DUT
behavior and trustworthy observation, not new candidate-visible VISA methods:

```text
before_command
dynamic_response
after_command
on_error
```

No recognized SCPI command may bypass `SimVisaLibrary`, its session, or
PyVISA-sim's native command matching. Hooks may validate preconditions, provide
world-dependent responses after native recognition, validate committed state,
and record errors exactly as in the approved proof of concept.

## Wire Protocol

The transport is `AF_UNIX` plus `SOCK_STREAM`. Every frame is:

```text
4-byte unsigned big-endian payload length
UTF-8 JSON payload
```

The maximum payload is 1 MiB. Zero-length, oversized, truncated, invalid UTF-8,
invalid JSON, duplicate-field, wrong-version, and wrong-shape frames are
connection-level rejections.

Requests contain exactly:

```json
{
  "version": 1,
  "request_id": 42,
  "operation": "read",
  "args": {"session": "opaque-token", "count": 4096}
}
```

Successful responses contain exactly:

```json
{
  "version": 1,
  "request_id": 42,
  "ok": true,
  "result": {"bytes_b64": "..."},
  "status": 0
}
```

Controlled request rejections contain exactly:

```json
{
  "version": 1,
  "request_id": 42,
  "ok": false,
  "error": {"kind": "invalid_session", "code": -1073807346,
            "message": "invalid session"}
}
```

The concrete protocol implementation defines an exact per-operation schema.
Values use a closed tagged union of null, boolean, integer, finite float,
string, bytes-as-base64, and lists of those types. Arbitrary Python objects,
pickle, YAML, module names, filesystem paths, tracebacks, and hidden exception
data never cross the socket.

`request_id` is connection-local and monotonically increasing in the public
backend. The broker echoes it but does not use it as authority. Unknown keys or
operations are rejected. Status codes are stable PyVISA integer values and are
passed through PyVISA's normal local `handle_return_value` path.

A protocol `hello` operation negotiates only the protocol version and supported
operation names. It contains no simulator version, YAML identity, world ID,
device mapping, DUT value, evidence path, or health detail. The outer evaluator
uses the same handshake for readiness.

## Session Ownership and Concurrency

Every accepted connection receives a random connection ID. Every remote
resource-manager or resource token is cryptographically random and bound to:

- the connection that created it;
- the peer credentials observed at connect time;
- the real PyVISA-sim session held only by the broker.

Tokens cannot be used on a second connection, after explicit close, or after
disconnect. A close is idempotent only where original PyVISA/PyVISA-sim
semantics permit it; the broker does not invent success for invalid sessions.

The public backend uses one in-flight request per persistent connection. The
broker may serve multiple connections, but serializes access to shared
PyVISA-sim and `BenchContext` state with a trusted lock. Connection and request
limits prevent a candidate from exhausting broker threads or file descriptors.
Limit rejection is scoped to the offending connection and is journaled.

When a connection disappears, the broker:

1. freezes new work for that connection;
2. records the still-owned sessions;
3. closes each real session;
4. records `forced_session_cleanup` events separately from candidate `close`;
5. invalidates all tokens owned by the connection.

Forced session cleanup never satisfies the `active_close_all` gate.

## Error and Retry Semantics

Three disjoint classes prevent an adversarial candidate from manufacturing a
retryable run.

### VISA result/status

Normal simulator outcomes, including timeout, unsupported attribute, and
invalid session, return the original PyVISA-sim result/status pair. The local
backend feeds that status through PyVISA's standard handling. These outcomes
are candidate-visible and are graded normally.

### Connection-local rejection

Malformed frames, excessive frames, unknown operations, invalid argument
shapes, guessed tokens, cross-connection session use, and peer/request limit
violations close or reject only the offending connection. They are recorded in
the trusted journal and count as candidate behavior. They do not set a fatal
marker, stop the sim, or make the world retryable.

If the public backend loses its transport, it presents a standard PyVISA system
error to candidate code. This presentation does not decide retry eligibility.

### Trusted fatal infrastructure failure

The sim writes a private `fatal.json` marker and terminates non-zero when it
cannot preserve trusted execution, including:

- socket bind or readiness failure;
- protocol implementation invariant failure;
- uncaught broker, PyVISA-sim, hook, or DUT exception;
- journal write, hash-chain, snapshot, or summary failure;
- signal finalization or force-safe failure.

The outer evaluator also treats unexpected sim exit, missing/invalid evidence,
container-policy mismatch, or failed container cleanup as infrastructure
failure. Only trusted health, marker, evidence, and Docker inspection determine
`infrastructure_valid` and `retry_eligible`.

Candidate exception, non-zero exit, timeout, OOM, output overflow, malformed
result, deliberate bad socket traffic, or leaked sessions are non-retryable
candidate outcomes as long as the sim finalizes and evidence remains valid.

## Complete Event Journal

The hidden journal is append-only and hash-chained. It records the complete
ordered event stream; a scoring projection may normalize events, but must not
replace the raw journal.

Required event families include:

- sim process start, configuration digest, socket bind, ready, signal, freeze,
  finalize, force-safe, summary, and exit;
- connection accept/reject/disconnect with peer credentials and connection ID;
- every RPC request, validated typed arguments or their safe digest, result
  type/length/digest, returned VISA status, latency, and rejection;
- resource-manager/resource session open, explicit close, invalid access, and
  forced disconnect cleanup;
- every SCPI write/read boundary and exact byte length/digest;
- existing `before_command`, native match, `dynamic_response`,
  `after_command`, and `on_error` hook outcomes;
- local/cross-device precondition decisions, committed device transitions,
  error-queue changes, DUT transitions, and acquisition observations;
- cleanup pre-snapshot, force-safe actions, cleanup post-snapshot, and safe
  final-state decision.

Secret values are represented by deterministic digests, safe types, lengths,
or approved public values. Redaction must not delete an event or collapse
multiple events into one. The final summary contains the terminal journal hash,
event/RPC/SCPI counts, open and leaked sessions, pre/post-cleanup snapshots,
fatal state, and `safe` flag.

The evaluator validates sequence continuity, previous-hash linkage, event
counts, terminal hash, summary linkage, and required lifecycle events before
grading. Invalid or incomplete evidence is an infrastructure failure.

## Per-World Lifecycle

The outer evaluator owns this state machine:

```text
CREATE_WORLD_ROOT
-> WRITE_HIDDEN_WORLD
-> CREATE_SIM_CONTAINER
-> START_SIM_CONTAINER
-> WAIT_FOR_SOCKET_AND_HELLO
-> CREATE_CANDIDATE_CONTAINER
-> START_CANDIDATE_CONTAINER
-> STREAM_AND_WAIT_CANDIDATE
-> INSPECT_AND_COLLECT_CANDIDATE
-> STOP_AND_REMOVE_CANDIDATE
-> SIGNAL_SIM_SIGTERM
-> WAIT_FOR_SIM_FINALIZATION
-> INSPECT_AND_COLLECT_SIM_EVIDENCE
-> VERIFY_JOURNAL_AND_FINAL_STATE
-> REMOVE_SIM_CONTAINER
-> ORACLE_AND_GRADE
```

On every branch after candidate creation, the evaluator first stops, inspects,
collects, and removes the candidate before stopping the sim. This ensures no
candidate process can issue new operations during trusted finalization.

`SIGTERM` is the only normal finalization trigger. The sim signal handler stops
accepting new requests, drains or rejects in-flight work deterministically,
closes leaked sessions, captures the cleanup pre-state, executes force-safe,
captures the post-state, flushes the journal, writes the summary, and exits.

If the candidate was never created because sim readiness failed, the evaluator
still inspects and removes the sim and reports infrastructure failure. If the
candidate creation/start path fails, the evaluator finalizes the already-ready
sim and retains its evidence.

No container is removed before its inspect data, stdout/stderr hashes, exit/OOM
state, and cleanup result are captured.

## Container Policies

### Sim sibling

The sim uses:

```text
--network none
--read-only
--user 11001:11001
--cap-drop ALL
--security-opt no-new-privileges
--pids-limit <trusted bounded value>
--memory <trusted bounded value>
--memory-swap <same value>
--cpus <trusted bounded value>
--ulimit nofile=<bounded value>
```

Only transport, evidence, world, and bounded temporary mounts are allowed. The
sim receives no candidate source/output, Git metadata, host Python paths,
devices, Docker socket, or host PID/IPC/network namespace.

### Candidate sibling

The candidate retains the v1-strength restrictions:

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

The candidate receives only the visible instance bundle, submitted solution,
read-only transport mount, bounded `/output`, and bounded `/tmp`. It receives
no sim evidence, hidden world, evaluator image filesystem, service source,
Docker socket, secrets, Git metadata, or host namespaces.

### Outer evaluator

The outer evaluator retains UID 11001, read-only root, dropped capabilities,
no-new-privileges, and `network=none`. Its exceptional authority is one exact
Docker socket mount. It creates only containers bearing its run/world labels
and performs stale cleanup only within those labels.

The host orchestrator injects the exact evaluator image ID and same-path host
run root. The outer evaluator rejects a missing/mismatched image ID rather than
falling back to a mutable tag.

## Scoring and Report Integration

The v2 evaluator uses the current capability weights unchanged:

```text
discovery       15
driver          15
causal_state    20
experiment      25
safety          10
robustness      15
```

It runs the same nine fixed world families and ten deterministic repeated
worlds. The world adapter maps the v1 scenario dimensions into v2's hidden
PyVISA-sim/DUT configuration without exposing them to the candidate.

The complete v2 journal is normalized into the semantic evidence consumed by
the existing oracle and partial-order constraints. Normalization is a pure
trusted projection. It must preserve source sequence IDs so every score or
constraint points back to raw events.

The existing strict semantics remain:

- all fixed worlds strict-pass;
- at least 90% of repeated worlds strict-pass;
- five devices are discovered/accessed correctly;
- oracle agreement and final decision are correct;
- final state is safe;
- sessions are actively closed rather than only force-closed;
- forbidden access is absent;
- infrastructure evidence is valid.

v1 continues to emit report schema version 1 with its existing per-world
`container_evidence`. v2 uses a separate report schema version and emits, for
each world:

- `candidate_container_evidence`;
- `sim_container_evidence`;
- candidate artifact evidence;
- sim journal and summary evidence;
- `infrastructure_valid` and `retry_eligible`;
- normalized score/constraint evidence referencing raw journal sequences.

Top-level orchestration continues to record the outer
`evaluator_container_evidence`. Thus the three runtime roles are never
conflated. `instrument` selects the v1 or v2 validator from the declared
evaluator/report schema; it does not weaken v1 or accept one schema under the
other evaluator ID.

## Compatibility and Non-Goals

This design intentionally does not:

- modify the v1 instance, evaluator ID, candidate API, worlds, or report;
- make `@iab` implicit through `PYVISA_LIBRARY` or another environment default;
- allow `pyvisa.ResourceManager()` without the explicit backend selector to be
  considered compliant candidate code;
- implement VISA features absent from original PyVISA-sim;
- expose a benchmark-specific high-level instrument client as the formal v2
  API;
- expose device roles, resource names, hidden world values, service errors,
  journal, snapshot, score, or oracle through RPC;
- allow candidate-controlled finalize, reset-world, health-detail, snapshot,
  or evidence operations;
- reuse a sim or candidate container, connection, session, world, journal, or
  Python process between worlds;
- support Docker Desktop as a formal target in the first implementation.

## Acceptance Tests

### Public backend unit tests

- PyVISA discovers `pyvisa_iab` through `ResourceManager("@iab")`.
- The backend rejects a missing/invalid endpoint and protocol mismatch without
  revealing hidden configuration.
- Every supported method encodes exact arguments and decodes exact result and
  status types.
- Byte payloads, counts, attributes, local handles, remote tokens, close, and
  transport failure are covered.
- Unsupported `VisaLibraryBase` methods retain normal unsupported behavior.

### Local-versus-remote parity tests

For the same simulator YAML and command sequence, compare pinned local `@sim`
with remote `@iab` for:

- resource listing and resource-class use;
- open/close and invalid-session behavior;
- writes, reads, pending responses, exact bytes, byte counts, and status codes;
- get/set timeout and termination-related attributes;
- standard query, raw, ASCII, and binary helpers;
- chunked binary reads and termination variants;
- `disable_event`/`discard_events` no-op behavior;
- failure of event/SRQ/async/trigger/lock operations not supplied by sim.

### Protocol and adversarial tests

- partial frame, truncated frame, invalid UTF-8/JSON, unknown/duplicate keys,
  wrong version, invalid tagged value, and over-1-MiB frame;
- token guessing, token reuse after close, cross-connection token theft, wrong
  peer UID, and request-ID anomalies;
- repeated connection churn, connection/request limit, concurrent resource use,
  and disconnect with leaked sessions;
- direct malicious socket traffic cannot stop the broker, read evidence, alter
  the world, or make the run retryable;
- public errors and hello metadata contain no hidden path, module, YAML, role,
  DUT, world, oracle, or traceback detail.

### Journal tests

- every RPC and SCPI operation has ordered raw events, including rejection and
  cleanup paths;
- hash-chain continuity, event count, terminal hash, and summary link validate;
- candidate close and forced disconnect close remain distinguishable;
- redacted secrets retain one event per occurrence with deterministic digest;
- scoring projections reference raw sequence IDs;
- missing, reordered, modified, truncated, or extra-after-summary events fail
  infrastructure validation.

### Container integration tests on native Linux

- candidate and sim both use `network=none`, read-only roots, fixed UIDs,
  dropped capabilities, no-new-privileges, bounded resources, and exact mount
  allowlists;
- neither sibling sees `/var/run/docker.sock`;
- candidate sees the socket and visible bundle but cannot see world/evidence or
  replace the socket;
- sim sees its world/evidence but not candidate source/output;
- the outer evaluator launches sim by its own exact image ID;
- readiness, normal completion, candidate exception, timeout, OOM, output
  overflow, invalid result, candidate kill, sim early exit, and finalization
  failure follow their required cleanup and retry classifications;
- every path removes candidate first and sim second after evidence inspection;
- post-finalization state is force-safe.

### Formal three-repository tests

- v2 instance/evaluator IDs, protocol versions, report schema, visible hashes,
  Dockerfile/lock hashes, and image provenance match across repositories;
- the reference candidate uses the literal
  `pyvisa.ResourceManager("@iab")` and strict-passes all nine fixed and ten
  repeated worlds;
- declared negative candidates fail their intended gates;
- v2 reports contain valid outer, candidate, and sim container evidence plus a
  valid complete journal;
- infrastructure failures set `infrastructure_valid=false` and
  `retry_eligible=true` without a candidate capability conclusion;
- candidate-caused protocol abuse and normal candidate failures remain
  non-retryable;
- all existing v1 unit, hidden-world, container, schema, and formal orchestration
  tests continue to pass unchanged.

## Implementation Boundary

Implementation must proceed test-first and in cross-repository dependency
order:

1. freeze the public protocol and backend parity tests;
2. implement the public `pyvisa_iab` package and candidate-only v2 image;
3. migrate the trusted v2 service into the evaluator and add the low-level
   broker;
4. add dual-sibling lifecycle, journal verification, and v2 report schema;
5. add v2 instrument contracts/orchestration and provenance validation;
6. run native-Linux isolation, adversarial, formal scoring, and v1 regression
   suites.

The implementation plan must name exact files and test commands in each
repository. It may refine internal class/module names, but it may not change the
approved trust boundary, explicit `@iab` requirement, original PyVISA-sim
capability ceiling, complete-event requirement, lifecycle ordering, v1/v2
coexistence, or retry classification without a new design review.
