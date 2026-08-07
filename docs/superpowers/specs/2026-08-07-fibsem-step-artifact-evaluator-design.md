# FIBSEM Step Artifact Evaluator Design

**Status:** Accepted for implementation on 2026-08-07

## Purpose

Replace the current binary `fibsem_liftout_v1` step scoring implementation with
a fine-grained evaluator whose numeric score is derived primarily from the
trusted files exported at each checkpoint. The evaluator keeps the existing
source, instance, evaluator, candidate API, and four-step workflow identities.
It continues to enforce the trusted event journal, necessary partial order,
runtime isolation, and terminal safety as non-compensable gates.

The scored evidence is the evaluator-owned checkpoint export, not files written
by the candidate. Candidate-authored notes and files remain diagnostic only.

## Current limitation

The current evaluator computes trusted geometry metrics from an in-memory
simulator snapshot and awards each step either its full value or zero. Complete
artifact bundles receive a separate fixed score. This proves key connectivity
and ordering constraints, but it does not distinguish a nearly correct cut from
a poor cut, compare the resulting shape with a scenario-specific reference, or
explain how volume, local morphology, joint quality, and pose contributed to a
step score.

## Goals

- Score each step from trusted `sample.stl`, `deposition.stl`, component STL,
  `scene.stl`, `scene.glb`, SEM/FIB PNG, and `checkpoint.json` evidence.
- Combine volume similarity with component- and ROI-level shape comparison.
- Use volume IoU, bidirectional mean surface distance, and Hausdorff distance.
- Compare against both scenario-defined physical constraints and a private
  reference bundle generated for the exact scenario.
- Award weighted partial credit inside a step.
- Apply explicit score caps when critical physical states are wrong.
- Preserve journal order, runtime isolation, forbidden-access, and terminal
  safety as strict gates.
- Produce an auditable report containing raw metrics, criterion scores, caps,
  reference identities, and algorithm parameters.
- Remain deterministic under STL record ordering and equivalent ASCII/binary
  representations.

## Non-goals

- Candidate-authored STL files are not trusted or scored.
- The candidate is not required to reproduce the reference action sequence.
- Exact triangle-for-triangle equality with the reference is not required.
- Free ICP alignment is not used to hide a placement error.
- The public OpenFIBSEM API and four checkpoint names are not changed.
- The evaluator ID is not forked and no legacy scoring mode is retained.

## Fixed identities and compatibility boundary

The replacement keeps:

```text
source_id    = openfibsem
instance_id  = fibsem_liftout_v1
evaluator_id = fibsem_liftout_v1
checkpoints  = step_1, step_2, step_3, step_4
```

The candidate entrypoint, public `fibsem_iab` API, artifact directory names,
world IDs, and instrument configuration path remain stable. The evaluator
report schema changes from version 4 to version 5. There is no compatibility
fallback to the binary step scorer under the same evaluator ID.

## Architecture

### Data flow

```text
candidate action
  -> checkpoint(step_id)
  -> freeze immutable trusted simulator snapshot
  -> export trusted candidate checkpoint bundle
  -> verify export identity, hashes, formats, and snapshot agreement
  -> load and verify the exact-scenario private reference bundle
  -> derive scenario and reference-difference ROIs
  -> parse and canonicalize component STL meshes
  -> compute volume, IoU, surface, topology, connectivity, and pose metrics
  -> score weighted step criteria
  -> apply critical-state caps
  -> combine artifact evidence score
  -> attach journal/runtime/safety gates
  -> emit schema-version-5 report
```

### Trusted candidate checkpoint bundle

For each world and step, the scoring input is:

```text
artifacts/<world_id>/<step_id>/
  scene.glb
  scene.stl
  sem.png
  fib.png
  checkpoint.json
  components/
    source.stl
    sample.stl
    needle.stl
    target.stl
    deposition.stl
```

The checkpoint exporter writes this directory only after freezing the trusted
snapshot. The evaluator verifies file hashes, identities, component bounds,
and the canonical snapshot geometry hash before any numeric scoring. Candidate
files under `output_dir` never enter this path.

### Trust boundary

Three layers protect the score:

1. `checkpoint.json` binds the world, step, journal sequence, journal hash,
   geometry hash, artifact sizes, and artifact SHA-256 values.
2. Component STL, merged STL, and GLB identities and bounds must agree with
   each other.
3. Exported component geometry must agree with the immutable simulator
   snapshot captured before serialization.

A candidate cannot receive shape points by writing or replacing an STL. The
candidate container has no mount containing private reference bundles.

## Private reference bundles

### Bundle set

The evaluator owns one bundle for every member of the fixed ten-world suite:

```text
reference_artifacts/
  nominal/
  small/
  large/
  needle_offset/
  target_pose/
  seeded_01/
  seeded_02/
  seeded_03/
  seeded_04/
  seeded_05/
```

Each world contains:

```text
baseline/
  sample.stl
step_1/
  sample.stl
  deposition.stl
step_2/
  sample.stl
  deposition.stl
step_3/
  sample.stl
  deposition.stl
step_4/
  sample.stl
  deposition.stl
reference-manifest.json
```

Only meshes needed for morphology and material-delta comparison are stored in
the private reference bundle. Candidate checkpoint bundles continue to contain
all scene components for connectivity and consistency checks.

### Reference generation

Reference bundles are generated by running the evaluator-owned reference
solution against a fresh simulator world for each exact scenario. Candidate
and reference runs never share simulator state. The reference generation tool
supports:

```bash
python -m sources.openfibsem.fibsem_liftout_v1.reference_bundles build --all
python -m sources.openfibsem.fibsem_liftout_v1.reference_bundles verify --all
python -m sources.openfibsem.fibsem_liftout_v1.reference_bundles inspect \
  --world nominal --step step_2
```

Formal scoring loads pre-generated bundles instead of running the reference
workflow alongside each candidate. This makes the score faster and prevents
reference-state drift during a candidate run.

### Reference manifest

The manifest records:

- source, evaluator, scenario, and algorithm identities;
- OpenFIBSEM commit;
- evaluator commit used for generation as provenance;
- scenario SHA-256;
- reference solution SHA-256;
- generator source-tree SHA-256;
- mesh parser and shape algorithm versions;
- adaptive voxel and surface-sampling parameters;
- every reference file size and SHA-256;
- a bundle SHA-256 over the canonical file index.

The recorded evaluator commit is provenance rather than a self-referential
validation key. Runtime compatibility is established using the generator
source-tree digest, reference solution digest, OpenFIBSEM commit, scenario
digest, and algorithm version. This permits the generated bundles to be added
in a later commit without creating a circular commit identity.

Any incompatible manifest produces an infrastructure-invalid, retryable run;
it never becomes a candidate score.

## ROI model

### Scenario-defined hard ROIs

The scenario supplies:

- `sample.protected_region`;
- `sample.source_bridge`;
- `needle.joint_region`;
- `target.joint_region`;
- the work envelope for preflight and each step.

These ROIs define allowed locations and physical gates. They remain valid when
hidden worlds alter sample size, needle offset, or target pose.

### Reference-difference shape ROIs

Reference mesh differences define morphology comparison regions:

| Transition | Derived region |
|---|---|
| Baseline -> Step 1 | trench and U-cut removal region |
| Step 1 -> Step 2 | source-separation and needle-deposition regions |
| Step 2 -> Step 3 | target-deposition region |
| Step 3 -> Step 4 | needle-separation region |

The difference ROI is expanded by the relevant adaptive tolerance and clipped
to the scenario work envelope. Scenario ROIs are hard constraints; reference
difference ROIs only guide similarity scoring. If the generated reference
changes material outside its declared scenario envelope, reference validation
fails before candidate evaluation.

## Mesh evidence and canonicalization

Every parsed STL produces `MeshEvidence` containing:

```text
file_sha256
canonical_geometry_sha256
triangle_count
vertex_count
connected_component_count
watertight
non_manifold_edge_count
degenerate_triangle_count
bounds_um
volume_um3
surface_area_um2
centroid_um
```

Canonicalization:

- rejects non-finite and out-of-contract coordinates;
- welds vertices using a scenario-adaptive epsilon;
- discards no geometry silently;
- records degenerate triangles and non-manifold edges;
- recalculates geometry from vertices instead of trusting STL normals;
- sorts canonical vertices, faces, components, and evidence keys;
- makes ASCII and binary STL encodings of the same geometry equivalent.

Semantic topology defects are candidate result evidence. Serialization,
digest, identity, and trusted-snapshot mismatches are infrastructure failures.

## Shape comparison

### Coordinate handling

Morphology is compared in the named component or ROI frame. Pose is scored
separately in world/target coordinates. No unrestricted ICP is performed.
This prevents pose error from being hidden while avoiding double-penalizing a
placement error as both morphology and pose.

### Adaptive resolution

For characteristic sample length `L`:

```text
h = clamp(0.02 * L, 0.1 um, 0.5 um)
N = clamp(ceil(surface_area / h^2), 2048, 32768)
```

`h` is the voxel edge and `N` is the deterministic surface sample count. The
voxel origin and extent come from the scenario/reference ROI, not candidate
bounds. Large ROIs are evaluated in deterministic blocks.

Surface samples use a mesh-digest-derived seed and area-weighted deterministic
triangle sampling. Reported numeric metrics are rounded to six decimal places.

The public supported-evidence limits are:

```text
maximum artifact file size       = 64 MiB
maximum STL triangles per file   = 1,000,000
maximum input vertices per file  = 3,000,000
maximum welded vertices per file = 1,000,000
maximum voxel cells per ROI      = 4,194,304
maximum surface samples          = 32,768
```

The evaluator uses deterministic block processing within these limits. The
instance documentation publishes the same values so a complexity-limit score
is not based on a hidden threshold.

### Shape score formula

For candidate volume `Vc`, reference volume `Vr`, bidirectional mean surface
distance `d_asd`, Hausdorff distance `d_h`, and tolerance scale `tau`:

```text
volume_similarity = min(Vc, Vr) / max(Vc, Vr)
asd_score          = exp(-(d_asd / tau)^2)
hausdorff_score    = clamp(1 - d_h / (3 * tau), 0, 1)

shape_score =
    0.25 * volume_similarity
  + 0.35 * voxel_iou
  + 0.25 * asd_score
  + 0.15 * hausdorff_score
```

An empty/empty comparison is only valid for an ROI declared empty in both the
scenario and reference manifest. Any other empty candidate geometry receives
zero for that comparison.

Tolerance scale:

- sample and cut morphology use `position_tolerance_um`;
- joint morphology uses `joint_scale_um`;
- separation morphology uses the larger of `joint_scale_um` and the scenario
  minimum pattern feature.

## Step scoring

Step scores are weighted and allow partial credit. A step score is:

```text
final_score = min(raw_weighted_score, every_applicable_cap)
```

Cap reasons are stable, sorted identifiers. A missing checkpoint receives zero
and makes all later checkpoints ineligible, preserving the existing evidence
chain. An existing but poor earlier checkpoint does not automatically erase a
later step's diagnostic numeric score.

All steps also measure work-envelope compliance, collision state, and simulator
idle state from trusted evidence.

### Step 1: prepare the sample -- 20 points

| Criterion | Points |
|---|---:|
| Global `sample.stl` volume and morphology | 6 |
| Trench/U-cut reference-difference ROI morphology | 4 |
| Protection deposition ROI morphology | 3 |
| Source bridge geometry and sample-source connectivity | 3 |
| One principal sample component and at least 75% retained | 2 |
| Work envelope, no collision, and idle | 2 |

Caps:

| Condition | Maximum |
|---|---:|
| sample not connected to source | 8 |
| sample connected early to needle or target | 8 |
| sample fragmented or retained fraction below 65% | 5 |
| out-of-envelope material change or collision | 5 |
| missing/untrusted checkpoint evidence | 0 |

### Step 2: attach needle and release source -- 25 points

| Criterion | Points |
|---|---:|
| Carried sample volume and morphology preservation | 5 |
| Source-bridge separation ROI morphology | 5 |
| Needle-joint deposition morphology | 5 |
| Sample-needle connectivity and two-sided contact section | 5 |
| Sample/needle co-motion carry result | 3 |
| Work envelope, no collision, and idle | 2 |

Caps:

| Condition | Maximum |
|---|---:|
| sample still connected to source | 10 |
| sample not connected to needle | 10 |
| sample connected early to target | 8 |
| needle joint contacts only one side | 12 |
| retained sample fraction below 65% | 6 |
| missing/untrusted checkpoint evidence | 0 |

Carry requires both an ordered movement event and trusted Step 1/Step 2 mesh
pose evidence showing that the sample maintained its relationship to the
needle.

### Step 3: place and attach to target -- 25 points

| Criterion | Points |
|---|---:|
| Sample volume and morphology preservation | 4 |
| Target position and orientation | 5 |
| Target-joint deposition morphology | 5 |
| Needle/target dual connectivity and contact sections | 5 |
| Local sample-target interface alignment | 4 |
| Work envelope, no collision, and idle | 2 |

The pose criterion allocates three points to position and two to orientation.
It is full within tolerance, decays continuously between one and three times
tolerance, and is zero beyond three times tolerance.

Caps:

| Condition | Maximum |
|---|---:|
| sample not connected to target | 10 |
| sample disconnected early from needle | 10 |
| sample reconnected to source | 5 |
| position error above three times tolerance | 12 |
| target joint contacts only one side | 12 |
| retained sample fraction below 65% | 8 |
| missing/untrusted checkpoint evidence | 0 |

### Step 4: separate and retract needle -- 20 points

| Criterion | Points |
|---|---:|
| Final sample volume and morphology | 4 |
| Needle-separation ROI morphology | 4 |
| Preserved target-joint morphology and contact | 4 |
| Final source/needle/target connectivity topology | 4 |
| Final position and orientation | 2 |
| Safe needle retraction and idle state | 2 |

Caps:

| Condition | Maximum |
|---|---:|
| target joint disconnected | 6 |
| sample still connected to needle | 6 |
| sample connected to source | 4 |
| needle not safely retracted | 10 |
| retained sample fraction below 65% | 6 |
| position error above three times tolerance | 10 |
| missing/untrusted checkpoint evidence | 0 |

## Artifact evidence score -- 10 points

Each checkpoint bundle contributes 2.5 points:

| Criterion per checkpoint | Points |
|---|---:|
| Component STL parseability, finite geometry, and topology evidence | 0.75 |
| Merged scene STL/component consistency | 0.50 |
| GLB component, transform, hierarchy, material, and bounds consistency | 0.50 |
| SEM/FIB format, identity, non-constant content, contrast, and coverage | 0.75 |

The image subscore is deterministic and uses public thresholds recorded in the
report. It does not use a learned or network model.

Each beam contributes 0.375 points, split as follows:

| Image criterion per beam | Points | Full-credit rule |
|---|---:|---|
| PNG identity and dimensions | 0.125 | valid grayscale PNG at the exact scenario resolution |
| Robust contrast | 0.125 | `p95 - p05 >= 32` grayscale levels; linear credit from 0 to 32 |
| Useful-tone coverage | 0.125 | at least 10% of pixels are in `[5, 250]`; linear credit from 0% to 10% |

A constant image receives zero for contrast and useful-tone coverage. The
report records dimensions, p05, p95, robust contrast, and useful-tone fraction
for SEM and FIB independently.

Failure to request a checkpoint is a candidate outcome and earns no artifact
points for that checkpoint. Failure of the trusted exporter after a valid
checkpoint is infrastructure-invalid and retryable rather than a candidate
deduction.

## Journal, runtime, and safety gates

Numeric artifact scores remain diagnostic even when a non-compensable gate
fails. Strict pass requires the physical dependency chain:

```text
Preflight
  < destructive ROI operation
  < step_1
  < needle deposition
  < source separation
  < carry
  < step_2
  < transfer
  < target positioning
  < target deposition
  < step_3
  < needle separation
  < needle retraction
  < step_4
```

The world strict gates remain:

- preflight complete;
- journal integrity;
- necessary partial order;
- all checkpoint critical states;
- trusted artifact evidence complete;
- safe terminal state;
- no forbidden access;
- candidate runtime completed;
- runtime identities and isolation verified;
- no infrastructure failure.

Wrong order, forbidden access, unsafe terminal state, or isolation failure does
not erase useful STL metric diagnostics, but the world cannot strict-pass.

## Report schema version 5

Existing report concepts remain and the schema adds a detailed breakdown:

```json
{
  "schema_version": 5,
  "step_scores": {
    "step_1": 8.0,
    "step_2": 25.0,
    "step_3": 23.4,
    "step_4": 20.0
  },
  "step_breakdowns": {
    "step_1": {
      "raw_score": 17.4,
      "final_score": 8.0,
      "maximum": 20.0,
      "cap": {
        "applied": true,
        "maximum": 8.0,
        "reasons": ["sample_not_connected_to_source"]
      },
      "criteria": {
        "sample_shape": {
          "points": 4.8,
          "maximum": 6.0,
          "metrics": {
            "volume_similarity": 0.97,
            "voxel_iou": 0.82,
            "symmetric_surface_distance_um": 0.18,
            "hausdorff_distance_um": 0.62
          }
        }
      }
    }
  },
  "reference": {
    "scenario_digest": "1111111111111111111111111111111111111111111111111111111111111111",
    "bundle_digest": "2222222222222222222222222222222222222222222222222222222222222222",
    "algorithm_version": "stl-shape-v1"
  }
}
```

Every criterion contains a stable name, awarded points, maximum points, and raw
metrics. Reports record voxel size, sampling count, tolerance scale, candidate
component digest, and reference component digest for each shape comparison.

Top-level suite score remains the mean of ten numeric world scores. Dimension
scores remain the ten-world mean of each step and artifact dimension. Strict
suite pass still requires all five fixed worlds, at least four of five seeded
worlds, no unsafe terminal world, no forbidden access, no infrastructure
failure, and a suite score of at least 90.

## Error classification

Candidate-result failures that produce a normal score include:

- fragmented, empty, non-manifold, or semantically invalid result geometry;
- over-cut, under-cut, incorrect deposition, or poor reference similarity;
- insufficient joint contact section;
- excessive sample loss;
- wrong pose or connectivity;
- missing checkpoint;
- a result mesh that exceeds one of the exact public supported-evidence limits.

Infrastructure-invalid, retryable failures include:

- trusted export I/O or serialization failure after a valid checkpoint;
- reference bundle missing or incompatible;
- scenario/reference digest mismatch;
- snapshot/export identity or digest mismatch;
- post-export corruption;
- evaluator metric algorithm exception on evidence within supported limits;
- Docker, storage, or simulator infrastructure failure.

## Implementation units

```text
geometry/stl_mesh.py
  secure STL parsing, welding, topology, and MeshEvidence

geometry/voxel.py
  deterministic block voxelization and volume IoU

geometry/surface_distance.py
  deterministic surface samples, ASD, and Hausdorff distance

geometry/roi.py
  frame-aware clipping and reference material-difference ROIs

geometry/similarity.py
  normalized shape metrics and the shared shape score

reference_bundles.py
  bundle generation, manifest construction, loading, and verification

artifact_scoring.py
  candidate checkpoint bundle validation and artifact subscore

step_rubric.py
  criterion weights, pose curves, caps, and StepBreakdown types

scoring.py
  orchestration, journal/runtime gates, world and suite aggregation
```

The checkpoint exporter remains responsible only for producing deterministic
trusted artifacts. File parsing, shape metrics, and scoring stay independent
of exporter internals.

## Repository impact

### Evaluator repository

- add the geometry, reference, artifact, and rubric modules;
- generate and package private reference bundles;
- replace binary step score calculation;
- extend report dataclasses and validation to schema 5;
- update evaluator manifest to report schema 5;
- expand negative workflows and tests.

### Instrument repository

- accept and validate schema-version-5 FIBSEM reports;
- preserve detailed step breakdowns in the final report;
- update contract and acceptance tests;
- keep the existing configuration identity and paths.

### Instance repository

- document the weighted artifact-driven scoring behavior;
- document public shape metrics, tolerances, caps, image thresholds, and the
  exact supported-evidence limits;
- update `ACCEPTANCE.md` and visible-file hashes;
- keep the candidate API and scenario schema unchanged; the evidence limits
  are evaluator behavior documented in the visible scoring contract.

## Test strategy

### STL parser and topology tests

- equivalent binary and ASCII STL;
- reordered triangles and cyclic vertex permutations;
- reversed or missing normals;
- duplicate vertex welding;
- watertight, fragmented, non-manifold, and degenerate meshes;
- empty, truncated, oversized-count, NaN, Inf, and extreme-coordinate inputs.

### Shape metric tests

- identical shapes receive full similarity;
- equal volume with different shape cannot receive full similarity;
- equivalent morphology at a wrong pose separates morphology and pose scores;
- small perturbations produce continuous score changes;
- over-cut and under-cut reduce IoU, ASD, and Hausdorff scores;
- bounding-box overlap without physical contact does not create connectivity;
- a thin false bridge below joint scale is rejected.

### Rubric and cap tests

Each criterion and cap is tested independently for all four steps. Tests assert
raw score, applied cap, sorted cap reasons, final score, and strict gates.

### Metamorphic tests

Equivalent geometry retains its exact report metrics under STL record reorder,
triangle vertex-cycle change, binary/ASCII conversion, legal float formatting,
and component-file enumeration order.

### Adversarial tests

- candidate-authored reference copies do not enter trusted scoring;
- candidate metric claims in JSON do not affect trusted metrics;
- right volume/wrong shape loses shape points;
- enclosing geometry cannot forge connectivity;
- non-manifold sheets cannot forge a joint;
- GLB/STL and scene/component disagreement is detected;
- artifact and reference identity substitution is detected.

### Integration and formal acceptance

Completion requires:

- the reference solution scores 100 and strict-passes all ten worlds;
- repeated evaluation produces identical scores, caps, metrics, and reference
  digests;
- every declared negative triggers its intended criterion, cap, or gate;
- schema-version-5 reports pass evaluator and instrument validators;
- full host tests pass in all affected repositories;
- Linux dual-container acceptance passes;
- all forty checkpoint bundles remain available and read-only;
- the candidate container cannot access private reference artifacts.

## Migration sequence

1. Implement and verify canonical STL parsing and geometry primitives.
2. Implement deterministic shape metrics and ROI construction.
3. Implement reference bundle generation and verification.
4. Implement artifact scoring and the four step rubrics.
5. Replace world scoring while retaining journal/runtime gate logic.
6. Upgrade reports and instrument validation to schema 5.
7. Generate the ten private reference bundles.
8. Update public instance scoring documentation and hashes.
9. Run negative, deterministic, host, and Linux container acceptance suites.
