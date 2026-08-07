# FIBSEM Step Artifact Evaluator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `fibsem_liftout_v1` binary step scoring with deterministic, reference-aware STL/GLB/PNG/checkpoint scoring that awards weighted partial credit while preserving journal, isolation, and safety gates.

**Architecture:** The evaluator parses its own trusted checkpoint bundles, canonicalizes component STL geometry, compares component and ROI meshes with private exact-scenario reference bundles, then applies a four-step weighted rubric and critical-state caps. Existing journal/runtime gates remain independent strict-pass requirements; report schema 5 exposes every raw metric, criterion score, cap, and reference identity.

**Tech Stack:** Python 3.11+ standard library, existing OpenFIBSEM backend, JSON/YAML, pytest, Docker Linux acceptance. Do not add NumPy, SciPy, Trimesh, PyVista, or a network-fetched dependency to the evaluator package.

## Global Constraints

- Work directly in `/Users/britenyyyang/benchmark/{evaluator,instance,instrument}`; do not create a feature worktree.
- Keep `(source_id, instance_id, evaluator_id) = (openfibsem, fibsem_liftout_v1, fibsem_liftout_v1)`.
- Keep `step_1` through `step_4`, candidate entrypoint, public API, artifact paths, and ten-world suite identities unchanged.
- Candidate-authored artifacts are diagnostic only; score evaluator-owned trusted checkpoint exports.
- Numeric step scores use trusted files; necessary partial order, isolation, forbidden access, journal integrity, and terminal safety remain non-compensable gates.
- Step totals remain 20, 25, 25, and 20; artifact evidence remains 10 points.
- Report schema changes from 4 to 5 with no legacy scoring switch under the same evaluator ID.
- Shape score weights are volume 0.25, voxel IoU 0.35, ASD 0.25, and Hausdorff 0.15.
- Use `h = clamp(0.02L, 0.1 um, 0.5 um)` and `N = clamp(ceil(area/h^2), 2048, 32768)`.
- Enforce 64 MiB/file, 1,000,000 triangles/file, 3,000,000 input vertices/file, 1,000,000 welded vertices/file, 4,194,304 voxel cells/ROI, and 32,768 surface samples.
- Round report metrics to six decimal places and sort all externally visible mappings and cap reasons.
- Implement every behavior test-first and observe the intended failure before production edits.

---

### Task 1: Canonical STL parser and topology evidence

**Files:**
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/geometry/stl_mesh.py`
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/tests/test_stl_mesh.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/geometry/__init__.py`

**Interfaces:**
- Produces `StlLimits`, `MeshEvidence`, `CanonicalMesh`, `StlError`, `parse_stl(payload: bytes, *, limits: StlLimits = StlLimits(), weld_epsilon_um: float = 1e-6) -> CanonicalMesh`, and `parse_stl_path(path: Path, *, limits: StlLimits = StlLimits(), weld_epsilon_um: float = 1e-6) -> CanonicalMesh`.
- `CanonicalMesh.mesh` is the existing `TriangleMesh`; later tasks consume canonical vertices/faces and `MeshEvidence`.

- [ ] **Step 1: Write failing binary/ASCII equivalence and topology tests**

```python
def test_binary_and_ascii_box_have_identical_canonical_evidence() -> None:
    binary = parse_stl(binary_box_stl((0, 0, 0), (2, 4, 6)))
    ascii_mesh = parse_stl(ascii_box_stl((0, 0, 0), (2, 4, 6)))
    assert binary.evidence.canonical_geometry_sha256 == ascii_mesh.evidence.canonical_geometry_sha256
    assert binary.evidence.volume_um3 == pytest.approx(48.0)
    assert binary.evidence.watertight

def test_non_manifold_sheet_is_reported_not_silently_repaired() -> None:
    value = parse_stl(binary_stl_from_triangles(two_triangles_sharing_three_edges()))
    assert not value.evidence.watertight
    assert value.evidence.non_manifold_edge_count > 0
```

- [ ] **Step 2: Run tests and verify the missing-module failure**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_stl_mesh.py`

Expected: collection fails because `geometry.stl_mesh` does not exist.

- [ ] **Step 3: Implement secure parsing and canonicalization**

Implement these exact public types:

```python
@dataclass(frozen=True)
class StlLimits:
    maximum_file_bytes: int = 64 * 1024 * 1024
    maximum_triangles: int = 1_000_000
    maximum_input_vertices: int = 3_000_000
    maximum_welded_vertices: int = 1_000_000

@dataclass(frozen=True)
class MeshEvidence:
    file_sha256: str
    canonical_geometry_sha256: str
    triangle_count: int
    vertex_count: int
    connected_component_count: int
    watertight: bool
    non_manifold_edge_count: int
    degenerate_triangle_count: int
    bounds_um: Bounds
    volume_um3: float
    surface_area_um2: float
    centroid_um: Vec

@dataclass(frozen=True)
class CanonicalMesh:
    mesh: TriangleMesh
    evidence: MeshEvidence
```

Detect binary STL only when `84 + 50 * count == len(payload)`; otherwise parse a bounded ASCII grammar. Reject truncated records, non-finite floats, coordinates outside `[-1_000_000, 1_000_000]`, empty geometry, and limit violations. Weld rounded vertices, preserve and count degenerate faces, calculate undirected edge incidence, connected components, signed-volume magnitude, surface area, centroid, and a canonical digest independent of STL ordering and normals.

- [ ] **Step 4: Run the new parser tests and existing geometry tests**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_stl_mesh.py sources/openfibsem/fibsem_liftout_v1/tests/test_geometry.py`

Expected: all pass.

- [ ] **Step 5: Commit Task 1**

```bash
cd evaluator
git add sources/openfibsem/fibsem_liftout_v1/geometry/stl_mesh.py sources/openfibsem/fibsem_liftout_v1/geometry/__init__.py sources/openfibsem/fibsem_liftout_v1/tests/test_stl_mesh.py
git commit -m "feat: add canonical FIBSEM STL evidence"
```

---

### Task 2: Deterministic voxel and surface shape metrics

**Files:**
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/geometry/voxel.py`
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/geometry/surface_distance.py`
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/geometry/similarity.py`
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/tests/test_shape_similarity.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/geometry/__init__.py`

**Interfaces:**
- Consumes `CanonicalMesh`, `Bounds`, and `Vec` from Task 1/existing metrics.
- Produces `ShapeParameters`, `ShapeComparison`, `adaptive_shape_parameters`, `voxel_iou`, `surface_distances`, and `compare_shapes`.

- [ ] **Step 1: Write failing identity, equal-volume/wrong-shape, and determinism tests**

```python
def test_identical_box_scores_one() -> None:
    comparison = compare_shapes(box((2, 4, 6)), box((2, 4, 6)), roi, tau_um=0.5, characteristic_length_um=4.0)
    assert comparison.shape_score == 1.0
    assert comparison.voxel_iou == 1.0

def test_equal_volume_different_shape_loses_shape_points() -> None:
    comparison = compare_shapes(box((2, 2, 8)), box((4, 4, 2)), roi, tau_um=0.5, characteristic_length_um=4.0)
    assert comparison.volume_similarity == 1.0
    assert comparison.shape_score < 0.8

def test_triangle_order_does_not_change_metrics() -> None:
    assert compare_shapes(reordered(candidate), reference, roi, 0.5, 4.0) == compare_shapes(candidate, reference, roi, 0.5, 4.0)
```

- [ ] **Step 2: Run tests and verify missing functions fail**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_shape_similarity.py`

Expected: collection fails on missing similarity module.

- [ ] **Step 3: Implement standard-library deterministic algorithms**

Use the exact result type:

```python
@dataclass(frozen=True)
class ShapeComparison:
    candidate_volume_um3: float
    reference_volume_um3: float
    volume_similarity: float
    voxel_iou: float
    symmetric_surface_distance_um: float
    hausdorff_distance_um: float
    asd_score: float
    hausdorff_score: float
    shape_score: float
    voxel_size_um: float
    surface_sample_count: int
    candidate_geometry_sha256: str
    reference_geometry_sha256: str
```

Voxelize in deterministic z/y/x blocks using odd/even ray intersection at cell centers, with the scenario/reference ROI fixing the grid. Generate area-weighted barycentric surface samples from a digest-derived counter sequence. Use a cell-sized spatial hash that expands neighbor shells until the current best distance is bounded; compute bidirectional mean and maximum nearest-sample distances. Apply the accepted 0.25/0.35/0.25/0.15 formula and six-decimal rounding.

- [ ] **Step 4: Run shape, parser, and geometry tests**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_shape_similarity.py sources/openfibsem/fibsem_liftout_v1/tests/test_stl_mesh.py sources/openfibsem/fibsem_liftout_v1/tests/test_geometry.py`

Expected: all pass and the equal-volume/wrong-shape case scores below 0.8.

- [ ] **Step 5: Commit Task 2**

```bash
cd evaluator
git add sources/openfibsem/fibsem_liftout_v1/geometry sources/openfibsem/fibsem_liftout_v1/tests/test_shape_similarity.py
git commit -m "feat: score deterministic FIBSEM shape similarity"
```

---

### Task 3: Frame-aware ROIs and private reference bundle contract

**Files:**
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/geometry/roi.py`
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/reference_bundles.py`
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/tests/test_reference_bundles.py`
- Modify: `evaluator/pyproject.toml`

**Interfaces:**
- Consumes `ScenarioSpec`, `CanonicalMesh`, and canonical digests.
- Produces `RoiSet`, `ReferenceIdentity`, `ReferenceStep`, `ReferenceBundle`, `derive_roi_set`, `build_reference_bundle`, `load_reference_bundle`, and CLI `main(argv=None)`.

- [ ] **Step 1: Write failing manifest, tamper, and ROI tests**

```python
def test_reference_manifest_binds_scenario_algorithm_and_files(tmp_path: Path) -> None:
    bundle = build_fixture_bundle(tmp_path, nominal_spec())
    loaded = load_reference_bundle(bundle, nominal_spec())
    assert loaded.identity.algorithm_version == "stl-shape-v1"
    assert set(loaded.steps) == {"step_1", "step_2", "step_3", "step_4"}

def test_reference_file_tamper_is_infrastructure_error(tmp_path: Path) -> None:
    bundle = build_fixture_bundle(tmp_path, nominal_spec())
    (bundle / "step_2/sample.stl").write_bytes(b"tampered")
    with pytest.raises(ReferenceBundleError, match="digest"):
        load_reference_bundle(bundle, nominal_spec())

def test_reference_delta_roi_is_clipped_to_step_envelope() -> None:
    assert derive_roi_set(reference_steps, nominal_spec()).step_1_cut.bounds == expected_bounds
```

- [ ] **Step 2: Run tests and observe missing reference module failure**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_reference_bundles.py`

Expected: collection fails because `reference_bundles` is missing.

- [ ] **Step 3: Implement manifest and ROI derivation**

Use `reference-manifest.json` schema version 1 with exact scenario, OpenFIBSEM, generator-tree, reference-solution, algorithm, parameter, file, and bundle digests. `load_reference_bundle(root, spec)` validates every byte before parsing. `derive_roi_set` converts scenario boxes to world/component frames and forms expanded/clipped material-difference bounds for Step 1 cut, Step 2 source separation and needle deposition, Step 3 target deposition, and Step 4 needle separation.

Add package data:

```toml
"sources.openfibsem.fibsem_liftout_v1" = [
  "evaluator.yaml",
  "scenarios/*.json",
  "reference_artifacts/**/*.json",
  "reference_artifacts/**/*.stl",
]
```

- [ ] **Step 4: Run reference and scenario tests**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_reference_bundles.py sources/openfibsem/fibsem_liftout_v1/tests/test_scenario.py`

Expected: all pass.

- [ ] **Step 5: Commit Task 3**

```bash
cd evaluator
git add pyproject.toml sources/openfibsem/fibsem_liftout_v1/geometry/roi.py sources/openfibsem/fibsem_liftout_v1/reference_bundles.py sources/openfibsem/fibsem_liftout_v1/tests/test_reference_bundles.py
git commit -m "feat: define private FIBSEM reference bundles"
```

---

### Task 4: Trusted bundle scoring and evidence plumbing

**Files:**
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/artifact_scoring.py`
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/tests/test_artifact_scoring.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/geometry/artifacts.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/scoring.py`
- Modify: `evaluator/instrument_benchmark_evaluator/container/fibsem_sim_runner.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/tests/test_artifacts.py`
- Modify: `evaluator/tests/test_fibsem_sim_runner.py`

**Interfaces:**
- Extends `CheckpointEvidence` with non-serialized `artifact_root: Path | None` and serialized `artifact_evidence`.
- Produces `ArtifactCriterion`, `CheckpointArtifactScore`, and `score_checkpoint_artifacts(root, expected_world, expected_step, trusted_snapshot=None)`.

- [ ] **Step 1: Write failing trusted-path and cross-format tests**

```python
def test_checkpoint_evidence_retains_validated_artifact_root(tmp_path: Path) -> None:
    trusted = load_fibsem_evidence(valid_evidence_tree(tmp_path), run_id="r", world_id="nominal")
    assert trusted.checkpoints["step_1"].artifact_root == tmp_path / "artifacts/nominal/step_1"

def test_scene_component_mismatch_loses_consistency_points(bundle: Path) -> None:
    replace_scene_stl_with_shifted_mesh(bundle)
    score = score_checkpoint_artifacts(bundle, "nominal", "step_1")
    assert score.criteria["scene_component_consistency"].points == 0.0
```

- [ ] **Step 2: Run tests and verify missing scoring behavior**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_artifact_scoring.py tests/test_fibsem_sim_runner.py`

Expected: failures show missing `artifact_root` and scorer.

- [ ] **Step 3: Implement artifact subscore and safe path plumbing**

Score 0.75 STL topology evidence, 0.50 merged/component consistency, and 0.50 GLB hierarchy/material/transform/bounds consistency. For each of SEM and FIB, score 0.125 for a valid grayscale PNG at exact scenario resolution, 0.125 linearly from robust contrast `p95-p05` 0 through 32, and 0.125 linearly from useful-tone coverage 0% through 10% for pixels in `[5, 250]`; constant images receive zero for the latter two criteria. Keep digest, serialization, identity, or trusted-snapshot disagreement as `ArtifactError` infrastructure failures. Store only the relative artifact path in serialized reports; the absolute resolved `artifact_root` is runtime-only and must remain below the validated evidence root.

- [ ] **Step 4: Run artifact, exporter, sim-runner, and service tests**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_artifact_scoring.py sources/openfibsem/fibsem_liftout_v1/tests/test_artifacts.py sources/openfibsem/fibsem_liftout_v1/tests/test_exporter.py sources/openfibsem/fibsem_liftout_v1/tests/test_service.py tests/test_fibsem_sim_runner.py`

Expected: all pass.

- [ ] **Step 5: Commit Task 4**

```bash
cd evaluator
git add sources/openfibsem/fibsem_liftout_v1 instrument_benchmark_evaluator/container/fibsem_sim_runner.py tests/test_fibsem_sim_runner.py
git commit -m "feat: score trusted FIBSEM checkpoint bundles"
```

---

### Task 5: Weighted four-step rubric and caps

**Files:**
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/step_rubric.py`
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/tests/test_step_rubric.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/scoring.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/tests/test_scoring.py`

**Interfaces:**
- Produces `CriterionScore`, `ScoreCap`, `StepBreakdown`, `StepEvidence`, `score_step`, and stable criterion/cap identifiers.
- Consumes current/previous checkpoint meshes, geometry cross-checks, `RoiSet`, `ReferenceBundle`, and `ScenarioSpec`.

- [ ] **Step 1: Write failing partial-credit and cap tests**

```python
def test_step_1_wrong_source_connection_caps_otherwise_good_shape() -> None:
    result = score_step("step_1", good_step_1(sample_to_source=False))
    assert result.raw_score > 15.0
    assert result.final_score == 8.0
    assert result.cap.reasons == ("sample_not_connected_to_source",)

def test_step_3_pose_decays_between_one_and_three_tolerances() -> None:
    near = score_step("step_3", good_step_3(position_error=1.5 * tolerance))
    far = score_step("step_3", good_step_3(position_error=2.5 * tolerance))
    assert 0 < far.criteria["target_pose"].points < near.criteria["target_pose"].points < 5

def test_missing_step_3_checkpoint_zeroes_step_3_and_step_4() -> None:
    report = grade_world(
        nominal_spec(),
        valid_journal(),
        checkpoints_without("step_3"),
        safe_terminal(),
        valid_runtime(),
        reference=perfect_reference_bundle(),
    )
    assert report.step_scores["step_3"] == 0
    assert report.step_scores["step_4"] == 0
```

- [ ] **Step 2: Run tests and verify missing rubric failure**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_step_rubric.py sources/openfibsem/fibsem_liftout_v1/tests/test_scoring.py`

Expected: collection or assertions fail because weighted scoring is absent.

- [ ] **Step 3: Implement the accepted weights and cap tables**

Encode the exact design-document criterion totals and caps. Calculate each raw criterion independently, set `final_score = min(raw_score, applicable cap maxima)`, round to six decimals, and sort cap reasons. Preserve numeric STL diagnostics when journal/runtime gates fail. Critical-state gates use trusted STL-derived connectivity cross-checked against the frozen geometry metrics.

- [ ] **Step 4: Run rubric, scoring, geometry, and negative tests**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_step_rubric.py sources/openfibsem/fibsem_liftout_v1/tests/test_scoring.py sources/openfibsem/fibsem_liftout_v1/tests/test_geometry.py sources/openfibsem/fibsem_liftout_v1/tests/test_negatives.py`

Expected: all pass and step scores are floats with accepted maxima.

- [ ] **Step 5: Commit Task 5**

```bash
cd evaluator
git add sources/openfibsem/fibsem_liftout_v1/step_rubric.py sources/openfibsem/fibsem_liftout_v1/scoring.py sources/openfibsem/fibsem_liftout_v1/tests/test_step_rubric.py sources/openfibsem/fibsem_liftout_v1/tests/test_scoring.py sources/openfibsem/fibsem_liftout_v1/tests/test_negatives.py
git commit -m "feat: grade FIBSEM steps with weighted artifact rubrics"
```

---

### Task 6: Report schema 5 and evaluator runtime integration

**Files:**
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/scoring.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/reports.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/evaluator.yaml`
- Modify: `evaluator/instrument_benchmark_evaluator/fibsem_run.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/tests/test_reports.py`
- Modify: `evaluator/tests/test_fibsem_run.py`
- Modify: `evaluator/tests/test_dispatch.py`

**Interfaces:**
- `FibsemWorldReport.to_dict()` emits `step_breakdowns` and `reference`.
- `FibsemEvaluationReport.to_dict()` emits schema version 5.
- `run_fibsem_world` resolves the exact reference bundle before grading and turns reference/metric infrastructure errors into retryable `score=None` reports.

- [ ] **Step 1: Write failing schema-5 round-trip and infrastructure tests**

```python
def test_report_schema_version_5_round_trips_breakdowns() -> None:
    report = complete_report()
    validated = validate_report(report)
    assert validated["schema_version"] == 5
    assert validated["worlds"][0]["step_breakdowns"]["step_1"]["criteria"]["sample_shape"]["metrics"]["voxel_iou"] == 1.0

def test_reference_digest_mismatch_is_retryable_not_candidate_zero(tmp_path: Path) -> None:
    execution = run_fibsem_world(
        benchmark=benchmark_settings(tmp_path),
        instance=instance_settings(),
        spec=nominal_spec(),
        candidate_path=reference_candidate(),
        backend=successful_backend(),
        sim_runner=successful_sim_runner(tmp_path),
        reference_root=tampered_reference(tmp_path),
    )
    assert execution.report.score is None
    assert execution.report.retry_eligible
```

- [ ] **Step 2: Run tests and verify schema-4 mismatch**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_reports.py tests/test_fibsem_run.py tests/test_dispatch.py`

Expected: assertions fail because reports still emit schema 4 and no breakdown.

- [ ] **Step 3: Implement schema 5, runtime loading, and validators**

Validate exact top-level/world/breakdown/reference keys, numeric ranges, maximum totals, sorted cap reasons, six-decimal metrics, 64-character digests, and criterion sums. Change `report_schema_version` to 5. Keep suite mean and fixed/seeded strict gates unchanged.

- [ ] **Step 4: Run evaluator FIBSEM report/runtime tests**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests tests/test_fibsem_run.py tests/test_fibsem_sim_runner.py tests/test_dispatch.py`

Expected: all pass.

- [ ] **Step 5: Commit Task 6**

```bash
cd evaluator
git add sources/openfibsem/fibsem_liftout_v1 instrument_benchmark_evaluator/fibsem_run.py tests/test_fibsem_run.py tests/test_dispatch.py
git commit -m "feat: publish FIBSEM artifact report schema 5"
```

---

### Task 7: Generate and verify the ten private reference bundles

**Files:**
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/reference_artifacts/**`
- Create: `evaluator/scripts/generate_fibsem_reference_bundles.py`
- Create: `evaluator/sources/openfibsem/fibsem_liftout_v1/tests/test_reference_artifacts.py`
- Modify: `evaluator/sources/openfibsem/fibsem_liftout_v1/reference_bundles.py`
- Modify: `evaluator/.gitignore`

**Interfaces:**
- Generator consumes the instance nominal scenario, evaluator hidden scenarios, five deterministic seeds, pinned OpenFIBSEM checkout, and evaluator reference solution.
- Produces the exact private directory and manifests specified in the design.

- [ ] **Step 1: Write failing completeness/provenance test**

```python
def test_packaged_reference_artifacts_cover_exact_ten_world_suite() -> None:
    bundles = load_packaged_reference_bundles(all_suite_specs())
    assert set(bundles) == {"nominal", "small", "large", "needle_offset", "target_pose", "seeded_01", "seeded_02", "seeded_03", "seeded_04", "seeded_05"}
    assert all(set(bundle.steps) == set(STEPS) for bundle in bundles.values())
```

- [ ] **Step 2: Run and observe missing bundle failure**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_reference_artifacts.py`

Expected: fails because packaged bundles do not exist.

- [ ] **Step 3: Implement the isolated reference collection command**

The command runs every exact scenario in a fresh simulator, invokes only the evaluator-owned reference candidate, copies baseline and step component STL into a temporary bundle, builds the manifest, verifies it, then atomically replaces the destination. It refuses dirty evaluator/OpenFIBSEM inputs and records source-tree and solution digests.

- [ ] **Step 4: Generate the bundles on native Linux Docker**

Run:

```bash
cd evaluator
.venv/bin/python scripts/generate_fibsem_reference_bundles.py \
  --instance ../instance/sources/openfibsem/fibsem_liftout_v1 \
  --openfibsem ../fibsem \
  --output sources/openfibsem/fibsem_liftout_v1/reference_artifacts
```

Expected: ten verified bundles, each with baseline, four steps, and a valid manifest.

- [ ] **Step 5: Run packaged bundle and determinism tests**

Run: `cd evaluator && .venv/bin/python -m pytest -q sources/openfibsem/fibsem_liftout_v1/tests/test_reference_artifacts.py sources/openfibsem/fibsem_liftout_v1/tests/test_reference_bundles.py`

Expected: all pass; a second `verify --all` reports the same bundle digests.

- [ ] **Step 6: Commit Task 7**

```bash
cd evaluator
git add scripts/generate_fibsem_reference_bundles.py sources/openfibsem/fibsem_liftout_v1/reference_bundles.py sources/openfibsem/fibsem_liftout_v1/reference_artifacts sources/openfibsem/fibsem_liftout_v1/tests/test_reference_artifacts.py .gitignore
git commit -m "data: pin FIBSEM step reference meshes"
```

---

### Task 8: Instrument schema and publication integration

**Files:**
- Modify: `instrument/schemas/run.schema.json`
- Modify: `instrument/scripts/validate_fibsem_benchmark.py`
- Modify: `instrument/src/instrument_benchmark/orchestrator.py`
- Modify: `instrument/tests/test_fibsem_contracts.py`
- Modify: `instrument/tests/test_validation_script.py`
- Modify: `instrument/tests/test_orchestrator.py`

**Interfaces:**
- Instrument accepts only FIBSEM report schema 5 for this evaluator identity.
- Publication preserves `step_breakdowns`, `reference`, and forty artifact bundles without exposing private reference artifacts.

- [ ] **Step 1: Write failing schema-5 and private-reference-exclusion tests**

```python
def test_fibsem_request_and_validator_require_report_schema_5() -> None:
    validated = validate_fibsem_report(schema_5_fixture())
    assert validated["schema_version"] == 5

def test_published_artifacts_exclude_private_reference_bundles(tmp_path: Path) -> None:
    published = publish_fixture(tmp_path)
    assert not any("reference_artifacts" in path.parts for path in published.rglob("*"))
```

- [ ] **Step 2: Run and verify schema-4 expectation failures**

Run: `cd instrument && .venv/bin/python -m pytest -q tests/test_fibsem_contracts.py tests/test_validation_script.py tests/test_orchestrator.py`

Expected: FIBSEM schema assertions fail on current version 4 expectations.

- [ ] **Step 3: Implement exact schema-5 validation and publication**

Validate breakdown maximums, criterion point ranges, cap consistency, reference digests, and exact ten-world identities. Preserve report content and publish only candidate trusted checkpoint artifacts. Do not copy evaluator-private reference bundles into reports.

- [ ] **Step 4: Run instrument contract and orchestrator tests**

Run: `cd instrument && .venv/bin/python -m pytest -q tests/test_fibsem_contracts.py tests/test_validation_script.py tests/test_orchestrator.py tests/test_repository_layout.py`

Expected: all pass.

- [ ] **Step 5: Commit Task 8**

```bash
cd instrument
git add schemas/run.schema.json scripts/validate_fibsem_benchmark.py src/instrument_benchmark/orchestrator.py tests/test_fibsem_contracts.py tests/test_validation_script.py tests/test_orchestrator.py
git commit -m "feat: validate FIBSEM artifact report schema 5"
```

---

### Task 9: Public instance scoring contract and visible hashes

**Files:**
- Create: `instance/sources/openfibsem/fibsem_liftout_v1/docs/scoring.md`
- Modify: `instance/sources/openfibsem/fibsem_liftout_v1/ACCEPTANCE.md`
- Modify: `instance/sources/openfibsem/fibsem_liftout_v1/docs/artifacts.md`
- Modify: `instance/sources/openfibsem/fibsem_liftout_v1/docs/experiment-contract.md`
- Modify: `instance/sources/openfibsem/fibsem_liftout_v1/instance.yaml`
- Create: `instance/tests/test_fibsem_scoring_contract.py`

**Interfaces:**
- Public docs expose exact weights, shape formula, tolerance choice, caps, image thresholds, and supported-evidence limits without revealing hidden references.

- [ ] **Step 1: Write failing visible-contract test**

```python
def test_fibsem_scoring_contract_is_visible_and_hashed() -> None:
    manifest = load_instance_manifest(FIBSEM_INSTANCE)
    scoring = FIBSEM_INSTANCE / "docs/scoring.md"
    assert manifest["visible_files"]["docs/scoring.md"] == sha256(scoring.read_bytes()).hexdigest()
    text = scoring.read_text()
    for phrase in ("voxel IoU", "Hausdorff", "Step 1: 20", "1,000,000 triangles", "critical-state caps"):
        assert phrase in text
```

- [ ] **Step 2: Run and verify missing public contract failure**

Run: `cd instance && .venv/bin/python -m pytest -q tests/test_fibsem_scoring_contract.py`

Expected: fails because `docs/scoring.md` is absent.

- [ ] **Step 3: Write the accepted public scoring contract and refresh hashes**

Document all accepted weights/formulas/caps and distinguish numeric file score from strict journal/runtime gates. Add `docs/scoring.md` to `visible_files` and recompute hashes for every modified visible file. Do not disclose hidden scenarios, reference meshes, or reference digests.

- [ ] **Step 4: Run instance tests**

Run: `cd instance && .venv/bin/python -m pytest -q`

Expected: 59 or more tests pass, with only declared skips.

- [ ] **Step 5: Commit Task 9**

```bash
cd instance
git add sources/openfibsem/fibsem_liftout_v1 tests/test_fibsem_scoring_contract.py
git commit -m "docs: publish FIBSEM artifact scoring contract"
```

---

### Task 10: Full regression, adversarial, deterministic, and Docker acceptance

**Files:**
- Verify: all files created or modified by Tasks 1-9.
- Verify: `instrument/reports/openfibsem/fibsem_liftout_v1.json` when the formal acceptance command writes the report; do not add it to Git unless it is already tracked.

**Interfaces:**
- Produces final evidence that all explicit design requirements work across the three repositories and native Linux containers.

- [ ] **Step 1: Run complete host suites**

```bash
cd instance && .venv/bin/python -m pytest -q
cd evaluator && PYTHONINTMAXSTRDIGITS=0 PYTHONPATH=vendor/pyvisa-sim-iab:. .venv/bin/python -m pytest -q
cd instrument && .venv/bin/python -m pytest -q
```

Expected: zero failures in all three repositories.

- [ ] **Step 2: Run deterministic reference verification twice**

```bash
cd evaluator
.venv/bin/python -m sources.openfibsem.fibsem_liftout_v1.reference_bundles verify --all --json
.venv/bin/python -m sources.openfibsem.fibsem_liftout_v1.reference_bundles verify --all --json
```

Expected: byte-identical JSON output and identical ten bundle digests.

- [ ] **Step 3: Run the formal FIBSEM validation script**

```bash
cd instrument
.venv/bin/python scripts/validate_fibsem_benchmark.py --config configs/openfibsem/fibsem_liftout_v1.yaml
```

Expected: schema 5, ten worlds, score 100 for the reference solution, all strict gates true, and forty published checkpoint bundles.

- [ ] **Step 4: Run native Linux dual-container acceptance**

```bash
cd instrument
scripts/run_fibsem_linux_acceptance.sh configs/openfibsem/fibsem_liftout_v1.yaml
```

Expected: candidate UID 10001, simulator UID 11001, no network, private reference bundle inaccessible, reference strict pass, and declared negatives trigger their intended criterion/cap/gate.

- [ ] **Step 5: Audit completion against the design**

Check every goal, non-goal, formula, weight, cap, schema field, error classification, test class, and acceptance command in `evaluator/docs/superpowers/specs/2026-08-07-fibsem-step-artifact-evaluator-design.md`. Record any missing evidence as incomplete and continue implementation until each item is proven.

- [ ] **Step 6: Confirm repository state**

Run `git status --short --branch` in evaluator, instance, and instrument. Expected: no uncommitted task changes; the pre-existing `instance/.DS_Store` may remain untracked and must not be added.
