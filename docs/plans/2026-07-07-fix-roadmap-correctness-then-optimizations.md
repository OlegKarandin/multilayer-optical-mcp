# Fix Plan — inspection-roadmap findings, correctness first, then optimizations, then Phase 7

> **For agentic workers:** this is the **master triage/ordering plan**. Each batch below is
> executed as its own repo-convention TDD implementation plan in `docs/plans/` (like the
> existing phase plans), via superpowers:subagent-driven-development or
> superpowers:executing-plans. The per-finding ACTION text in `docs/inspection-roadmap.md`
> is the spec for each item.

## Context

`docs/inspection-roadmap.md` (current as of 2026-07-07, spot-verified against the code:
`adapter.py:179 reversed(uids)`, `synthesize.py mkdtemp`, `translate.py != 0.0` sentinel,
`gnpy==2.11.1` pin, `whatif.py:39` concat, unguarded `ip_link_capacity_gbps` in
`ip_routing.py:102`, `_synthetic_trx`/`_optical_nodes` all still present) records ~60 findings
across 8 stages. **None are fixed yet.** Phase 7 (validate/commit/reconcile,
`docs/plans/2026-06-07-phase-7-validate-commit-reconcile.md`) is **not implemented** and its
validator replays plans through `recompute_qot_under_loading` + `simulate_ip_routing` — the two
call paths carrying the highest-severity bugs. Building Phase 7 on them would certify wrong physics.

**Decisions:** (1) land **all fixes (correctness + optimizations) before Phase 7**;
(2) documentation-only actions included as one final batch.

Every physics-touching batch ends by re-pinning
`tests/gnpy_adapter/test_ground_truth_bridge.py` within `TOL_DB`. All tests run via
`conda run -n multilayer-optical-mcp pytest`.

Finding references use `S<stage>-<n>` (e.g. S4-2 = Stage 4 finding 2) and Stage-4 step letters (A–G).

---

## PART 1 — CORRECTNESS (priority order)

### Batch C1 — GNPy pin repair (do first; everything downstream re-pins against it)
- **S3-addendum (pin drift) [Medium-repro]** — `pyproject.toml:13` + `requirements.txt:2` declare
  `gnpy==2.11.1` but `synthesize.py` uses the 2.14+ API (`design_network`,
  `load_equipment(..., extra_configs)`) and the env runs 2.14.0. Bump both pins to `gnpy==2.14.0`,
  verify a clean install imports, re-pin ground-truth GSNR values. *Files:* `pyproject.toml`,
  `requirements.txt`. *Test:* existing suite green under the declared pin.

### Batch C2 — Physical-layer core (Stage 4 revised action plan A→B→C; the highest-value fixes)
Order within batch is the roadmap's value÷risk order; each step independently shippable.
1. **Step A — resolve the paired reverse OMS (S4-2 + S4-3) [High]** — `compute_qot` BACKWARD must
   walk the physically separate reverse OMS (`oms_<dst>_<src>`, matched by swapped src/dst) in
   natural order instead of `reversed(uids)` (`gnpy_adapter/adapter.py:178-179`). Restores
   asymmetric-degradation QoT and the backward ROADM `add_drop_osnr` penalty (min-gated
   `mode_feasible` currently optimistically wrong). *Test:* multi-OMS path, asymmetric NF on the
   reverse chain moves backward QoT only.
2. **Step B — propagate through the terminal drop ROADM (S4-4) [Medium]** — final drop ROADM is
   nobody's `chain[0]` (`synthesize.py:137-143`) so its drop-side penalty is omitted even forward.
   Include it in the propagated element list. **Also fix the failure-side mirror S8-3:** dst ROADM
   absent from `OMS.elements` ⇒ `oms_seq_asset_set` can't match it, so
   `inject_failure(("roadm_<dst>",))` no-ops — make crossing detection include the terminal ROADM.
   *Test:* importer-style path terminating at a ROADM shows the drop penalty; `inject_failure` on
   the dst ROADM downs the lightpath.
3. **Step C1 — probe by frequency (S4-5/A1) [High]** — thread `center_freq_hz` into
   `compute_qot`/`recompute_qot_under_loading` (`adapter.py:184, 365-368`); select probe by
   frequency, fall back to `mode_id` only when unambiguous. *Test:* several same-mode channels at
   different frequencies each get their own QoT.
4. **Step C2 — per-path interferer comb (S4-6/B1, also fixes S8-5) [High]** — replace
   `loading_from_model`'s global concat (`model/whatif.py:28-39`, no `union`, duplicate-frequency
   carriers → malformed NLI) with the per-path construction `allocation._build_loading` already
   uses: interferers = `occupied_along(state, lp.oms_sequence)`. No new `Channel.lightpath_id`
   field needed. *Test:* lightpath whose only other channels sit on a disjoint fiber gets clean
   single-carrier QoT. Deferred sub-item: quantify express-path over-count before ever building a
   per-OMS splice.
5. **TX launch power separation (S2-3 + S3-9) [High-bias]** — `build_si_for_loading`
   (`translate.py:110`) builds `tx_power_w` from the −20 dBm ROADM pch default → TX OSNR noise
   floor 20 dB too optimistic; `gnpy_design_network` (`synthesize.py:179/182`) conflates the same
   two concepts. Introduce `tx_launch_power_dbm` (default 0 dBm) distinct from the pch default,
   use it in both places. *Test:* ground-truth re-pin; assert tx noise floor scales with the new
   parameter, not with pch.

### Batch C3 — State-engine integrity (Stage 1)
1. **S1-9 snapshot corruption [High]** — `SnapshotStore.get()` returns the live stored model.
   Per roadmap ACTION: `_frozen` flag on `NetworkModel`, `_check_mutable()` guard in every mutator,
   `get()` returns a frozen clone, `branch()` stays unfrozen. **Build `NetworkModel.clone()` here
   as the single home for deep-copy** — Phase 7 Task 1 expects exactly this method; doing it now
   pre-completes that task. *Files:* `model/network.py`, `model/snapshots.py`.
2. **S1-7 stale QoT after physics mutation [High]** — `set_lightpath_mode` clears that lightpath's
   QoT entry; `apply_nf_delta`/`apply_loss_delta` clear all QoT entries. Otherwise
   `ip_link_capacity_gbps` silently serves stale capacity. *Test:* mutate → capacity read raises/
   reports unknown until recompute.
3. **S1-4 OMS chaining check [Medium]** — `add_lightpath` asserts
   `oms[i].dst_node_id == oms[i+1].src_node_id`. Also protects S7-1 (`_lightpath_endpoints`).
4. **S1-5 grid validation at add-time [Medium]** — `NetworkModel` gets optional
   `grid: SpectrumGrid`; `add_lightpath` calls `grid.slot_of(lp.center_freq_hz)`.

### Batch C4 — What-if composition (Stage 8)
1. **S8-1 recompute resurrects failed lightpaths [High]** — `recompute_qot_under_loading` writes
   feasible QoT over `inject_failure`'s `-inf` sentinels and synthesis ignores `_failed_assets`.
   Fix: recompute skips/re-sentinels any lightpath crossing a failed asset (single guard in
   `adapter.py:356-371` using the same crossing predicate as `inject_failure`). *Test:* fail →
   degrade → recompute; service stays dropped.
2. **S8-2 spurious `crossed` [Medium]** — lightpaths absent from `before` get
   `feasible_before=True` (`whatif.py:169-170`); exclude them from the crossing set.
3. **S8-4 unify feasibility predicate [Low]** — derive `feasible_before` and `feasible_after`
   from one predicate (margin ≥ 0 ⇔ `mode_feasible`).
4. **S8-6 `clear_failed` desync [Low]** — clearing `_failed_assets` leaves `-inf` sentinels; drop
   the stale sentinels (or force a recompute) so the two stores can't disagree.

### Batch C5 — IP-layer totality (Stage 5)
1. **S5-4 `simulate_ip_routing` raises `LookupError` [High]** — wrap the capacity read
   (`ip_routing.py:102`) in the same guard `views.ip_topology_dict` uses; "no QoT recorded" becomes
   a distinct structured state, never an exception out of a read tool. **Phase 7 compatibility:**
   its Task 2 rewrite (`_link_is_up` catching `LookupError`) preserves this — note it in the batch
   plan so the later rewrite doesn't regress.
2. **S5-5 `down_links` misses idle-down links [Medium]** — populate `down_links` with every down
   link; report the loaded subset separately (or rename the field to match the contract).

### Batch C6 — Routing direction & candidate starvation (Stages 6 + 7)
1. **S6-4 undirected OMS graph offers reverse-direction OMS [High]** — `_oms_between` matches the
   unordered `{src,dst}` set (`solvers.py:124`), so `compute_paths(A,B)` emits B→A OMS and
   `compute_disjoint_paths` can return the two directions of one span as a "disjoint" pair. Filter
   enumeration to OMS whose `(src,dst)` matches travel direction (or make the graph directed); add
   an importer-style both-directions test. Feeds Step A's naming-based reverse resolution.
2. **S6-6 cap-32 starved by parallels [Medium]** — `_DISJOINT_CANDIDATE_CAP` counts
   (node-path × parallel-combo) emissions; cap by distinct node paths instead so parallels don't
   starve topological diversity (false `NO_SOLUTION`).
3. **S7-5 `NewLightpathRun` loses travel direction [Medium]** — record direction (or the reverse
   OMS id) in `NewLightpathRun`; `_lightpath_endpoints` on a reverse-traversed sequence currently
   reports swapped endpoints — a mis-oriented lightpath the moment Phase 7 provisions it.
4. **S7-6 `_PATH_BUDGET` burned by λ-duplicates [Medium]** — advance the 64-path budget only on
   distinct routes (dedup before counting) so λ-variants of one cheap route can't exhaust the
   frontier.
5. **S7-7 inconsistent dedup keys [Low]** — drop `r.lam` from `compute_restoration`'s cross-bucket
   key (`restoration.py:97-98`); a candidate never commits to a wavelength (roadmap point 4).

### Batch C7 — Synthesis & translation fidelity (Stage 3 + addendum + Stage 2)

> **STATUS (2026-07-10): all 7 items landed on master.** Each physics-touching commit was
> re-pinned against `test_ground_truth_bridge.py`. Items 1,3,4,5,6,7 moved GSNR by 0 (defaults
> equal the former hardcodes / all formats share 87.5 Gbaud). Item 2 (S3-11) intentionally
> shifted the toy GSNR (added drop-ROADM penalty) — no absolute value is pinned; the syn-vs-file
> gate holds. Full suite green.

1. ✅ **S3-1 + addendum-1 per-FiberType export [Medium]** — DONE. `model_to_gnpy_equipment` emits
   one `Fiber` per registered `FiberType` (dispersion/effective_area/pmd); importer registers a
   `FiberType` per distinct `fiber_type`. `FiberType.gamma` (dead) renamed to `effective_area`.
2. ✅ **S3-11 drop the `_synthetic_trx` branch (fixes S3-4 for free) [Medium]** — DONE **via
   Option B** (plan `docs/plans/2026-07-10-s3-11-symmetrize-toy-drop-synthetic-trx.md`).
   Symmetrized the toy (drop `ROADM Z`), migrated the 3 synthesis fixtures to the importer
   convention, and `_resolve_endpoint` now raises on an unresolvable endpoint. Option A (keep the
   toy asymmetric via a line-terminal transceiver) was empirically GSNR-neutral but rejected on
   principle: a transceiver with no ROADM is not a real optical terminal, even in a test.
3. ✅ **S3-5 per-ROADM `target_pch_out_db` [Medium]** — DONE.
4. ✅ **S3-6 amp tilt [Low]** — DONE.
5. ✅ **Addendum-2 span/NF misalignment [Medium]** — DONE (chose *fail-loud* over renormalise).
6. ✅ **S2-2 `power_dbm` sentinel [Medium]** — DONE (`Optional[float]`, `is not None`).
7. ✅ **S2-4 per-format symbol rate [Medium]** — DONE (per-format baud in `modes.py`; per-channel
   `baud_rate_hz`/`roll_off` in `build_si_for_loading`). *Residual: `roll_off` has no per-mode
   source (`TransceiverMode` lacks the field), so it falls back to the scalar 0.15.*

### Batch C8 — Small diagnostics & guards (Stage 4 E/F/G)

> **STATUS (2026-07-10): all 3 items landed on master.** GSNR unchanged
> (`test_ground_truth_bridge.py` re-pin holds within `TOL_DB` — Step E only affects
> genuinely single-channel band-edge probes, Step F only relabels the diagnostic).
> Full suite green (267 passed). Tests: `tests/gnpy_adapter/test_c8_diagnostics.py`.
> Step F resolved as **fix, not drop**: `limiting_element_id` *is* live tool surface
> (`server.compute_qot`/`recompute`/`breakdown` + `views.ip_topology_dict`), so the
> roadmap's "no CLAUDE.md tool reads it" premise was stale — corrected the min to
> select from finite deltas only.

1. ✅ **Step E — band-edge dummy (S4-1/A6) [Low]** — DONE. `_ensure_min_two_channels`
   places the dummy below the probe (returning channels frequency-ascending) when
   `probe + 100 GHz` would exceed the SI band edge `_SI_F_MAX_HZ` (196.1 THz).
2. ✅ **Step F — limiting-element diagnostic (S4-7/A5) [Low]** — DONE **via fix** (not
   drop). The min over GSNR deltas now guards `math.isfinite`, excluding the
   first-noise `inf→finite` transition (`finite - inf = -inf`) that previously made the
   booster always "limiting".
3. ✅ **Step G — verify `_find_launch_transceiver` (S4-8)** — DONE (verification-only,
   no production change). A Transceiver predecessor of the first path element exists
   for both the synthesized (importer) and file-loaded toy topologies.

### Batch C9 — Layered-graph parallel-OMS collapse (Stage 7, newly found)

> **STATUS (2026-07-10): landed on master.** `build_layered_graph` now returns an
> `nx.MultiDiGraph` (WLE keyed by `oms.id`, LPE by `lp.id`); `place_demands` enumerates over a
> `_collapse_to_simple` DiGraph (since `shortest_simple_paths` is not implemented for
> multigraphs) and `_parse_paths` re-expands per-hop parallel edges via `itertools.product`
> (mirrors the S6-4 collapse-then-expand). New tests: `test_parallel_oms_both_routes_enumerable`,
> `test_wle_count_counts_parallel_oms_per_layer`. **Bonus:** fixed a pre-existing
> generator-drain (confirmed 436s on master for the wide-grid S7-6 test) via a `_RAW_PATH_CAP`
> safety valve — neither the distinct-route `_PATH_BUDGET` nor the `_DEFAULT_K` placement guard
> bounds the raw `shortest_simple_paths` generator when distinct routes are few, so it enumerated
> every lambda-mixing simple path. Wide-grid test 436s → ~3s; full suite 269 passed in 31s. No
> physics touched (no ground-truth re-pin needed).

1. **S7-13 layered-graph parallel-OMS collapse [Medium]** — (new; found 2026-07-09 during the C6
   S7-6 fix, recorded as Stage 7 finding 13 in `inspection-roadmap.md`). `build_layered_graph`
   uses `nx.DiGraph`, so `g.add_edge((WL,a,lam),(WL,b,lam), oms_id=…)` for two parallel OMS on the
   same ordered node pair + slot overwrites the first — only the last-added parallel OMS's
   wavelength layer survives per λ, so `place_demands`/restoration silently considers one of
   several parallel fibers per wavelength. Fix: use a `MultiDiGraph` for the WL layers (mirrors the
   C6 S6-4 flat-solver change); add a two-parallel-OMS placement test asserting both routes are
   enumerable. Harmless on toy/demo topologies; a correctness issue once `solve_allocation`
   graduates onto the layered graph or a real multi-fiber topology loads. *Files:*
   `model/multilayer_graph.py`. *Test:* `tests/model/test_multilayer_graph.py`.

---

## PART 2 — OPTIMIZATIONS (after all correctness batches)

### Batch O1 — Cache the synthesized GNPy network/equipment (Stage 4 Step D + S3-7) — the real perf fix
`build_gnpy_network` runs on every `compute_qot` (2N per bulk recompute) and
`model_to_gnpy_equipment` leaks a `mkdtemp()` per call (`synthesize.py:43`, `:210`; one
`adv_nf_*.json` per distinct NF). Cache equipment+network per model-fingerprint, invalidate on
mutation (composes with C3's `_qot_state` invalidation hooks), scope temp dirs to the adapter
lifecycle. *Test:* bulk recompute over K lightpaths performs one synthesis; no orphaned temp dirs.
**Directly benefits Phase 7** — `validate_plan` recomputes per intermediate state.
**Explicitly deferred (do NOT build):** the findings-doc C2/C3 single-channel fast path / NLI
overload — wrong bottleneck, highest-risk surface; revisit only if post-O1 profiling shows NLI
itself dominates.

### Batch O2 — Hoist the offered-load map (S5-8 / S7-8) [Low]

> **STATUS (2026-07-10): landed on master.** `build_layered_graph` builds
> `offered_load_per_link(model)` once and passes the map into `_residual_gbps` (was rebuilt
> per lightpath, O(L·S)). `max`→`min` over a lightpath's bound IP links: chose **min** (the
> bottleneck link a groom is actually limited by); `max` overstated headroom a saturated
> sibling link can't provide. No behavior change on the live 1:1 model (min == max on a single
> link). No physics touched — no ground-truth re-pin. Full suite green (278 passed). New tests:
> `test_offered_load_map_built_once_per_graph_build`, `test_residual_is_min_over_bound_ip_links_not_max`.

`_residual_gbps` calls `offered_load_per_link(model)` inside `build_layered_graph`'s per-lightpath
loop (`multilayer_graph.py:80/119`) → O(L·S). Build once, pass in. While there: decide `max` vs
`min` over a lightpath's bound IP links for grooming residual (the `max` overstates when one link
is saturated) and document the choice.

### Batch O3 — Model hygiene cleanups (Stage 1 + Stage 3 leftovers)

> **STATUS (2026-07-10): all items landed on branch `o3-model-hygiene`.** Five commits,
> one per finding, TDD (failing test → fix → pass). Full suite 287 passed.
> - **S1-6** (subsumes **S1-3**): removed the dead `OpticalNode` class, `_optical_nodes`
>   dict, `add_optical_node`, the `clone()` line, and the importer call; dropped the
>   `_optical_nodes` line from the Phase 7 plan's `clone()` listing (line ~129). S1-3
>   resolved as **removal** (Literal moot once the class is gone). Note: the roadmap
>   listed `snapshots.py` as a site, but C3 already centralized cloning into
>   `NetworkModel.clone()`, so `snapshots.py` had no `_optical_nodes` reference to remove.
>   Test: `test_optical_node_shadow_registry_is_gone`.
> - **S1-1**: `RiskGroup.metadata` now wrapped in `MappingProxyType` via `__post_init__`
>   (defensive copy of the incoming mapping) — genuinely immutable, `dict()` consumers
>   unaffected. Test: `test_risk_group_metadata_is_read_only_and_defensively_copied`.
> - **S3-10**: new `gnpy_adapter/bands.py` with a `Band` value object; SI is the single
>   canonical literal (feeds `automatic_nch`), amp (widened) and transceiver (narrowed)
>   derived by named guard margins; import-time assert enforces amp ⊇ SI ⊇ transceiver.
>   `synthesize.py` and `adapter.py` (`_SI_F_MAX_HZ`) now source edges from the bands.
>   **GSNR-neutral**: every derived edge equals its former literal bit-for-bit — ground-truth
>   bridge unchanged, no re-pin. Tests: `tests/gnpy_adapter/test_bands.py`.
> - **S3-8**: resolved as **document** (not derive). Named the shared EDFA envelope
>   (`_EDFA_GAIN_FLATMAX_DB`/`_EDFA_GAIN_MIN_DB`/`_EDFA_P_MAX_DBM`) and documented the
>   single-homogeneous-envelope assumption; deriving per-amp bounds would move GSNR and no
>   reference amp nears the envelope. GSNR-neutral. Test:
>   `test_amps_share_one_documented_edfa_envelope_regardless_of_gain`.

1. **S1-6 remove `_optical_nodes` + `add_optical_node`** (written, cloned, never read) from
   `network.py`, `topology_import.py`, `snapshots.py`. **Also delete the `_optical_nodes` line from
   the Phase 7 plan's `clone()` listing** (Task 1, plan doc line ~129) so the plan doesn't
   resurrect it.
2. **S1-1 `RiskGroup.metadata`** — `MappingProxyType` (frozen dataclass with a mutable dict).
3. **S1-3 `OpticalNode.kind`** — constrain to `Literal["roadm","amplifier_site","transceiver_site"]`
   (moot if S1-6 removes the class entirely — check and prefer removal).
4. **S3-10 `Band` value object** — encode the ⊇ relationship of the three frequency bands
   (amp ⊇ SI ⊇ transceiver) with explicit guard-margin derivation; keep three distinct bands.
5. **S3-8 amp envelope** — document the single-envelope assumption (gain_flatmax 25 / p_max 23) or
   derive bounds from model amps.

---

## PART 3 — DOCUMENTATION & PINNING-TEST BATCH (Batch D)

> **STATUS (2026-07-11): landed on branch `batch-d-docs-and-pinning-tests`.** Nine
> docs-only commits (one per file/finding-group) plus the S2-1 pinning test, executed via
> `docs/plans/2026-07-10-batch-d-docs-and-pinning-tests.md` under
> superpowers:subagent-driven-development (fresh implementer + task reviewer per task, all
> nine reviews clean on first pass). No production logic changed anywhere in the batch —
> `synthesize.py`'s S3-2/S3-3 comments were re-verified against the ground-truth bridge
> test to confirm the physics literals (`33.0`, `[0.0, 0.0, 0.0, float(nf)]`) are
> byte-identical. **S7-9 confirmed NOT retired**: C7-7/S2-4 landed the
> `Channel.baud_rate_hz`/`roll_off` plumbing, but `allocation._build_loading` (used by both
> `allocation.py` and `multilayer_graph.place_demands`) still doesn't populate them from
> `ref_mode_id`, so the ref-mode QoT probe assumption documented at
> `multilayer_graph.py`'s `place_demands` is still live — independently verified by the
> Task 5 reviewer against `allocation.py`/`loading.py`/`translate.py`. Full suite: 288
> passed (287 baseline + the new S2-1 test), no regressions. Branch not yet merged to
> `master` — see the branch's own finishing-a-development-branch step.

One PR of docstrings + tests locking in invariants (roadmap ACTIONs verbatim):
- **S2-1** `test_union_allows_adjacent_channels` — pin strict-`<` overlap so a future `<=` can't
  break 50 GHz grids.
- **S1-8** `set_service_working_path` = statement of intended routing; `simulate_ip_routing`
  reports actual drops.
- **S5-2** `by_service` duplicate-LP-ID choice; **S5-6** per-direction asymmetry never reaches the
  IP layer (modeling boundary); **S5-7** `mark_failed` alone insufficient — the QoT sentinel is
  load-bearing (re-check wording after C4-1, which softens this); **S5-9** `overflow_gbps` and
  dropped demand must not be summed.
- **S6-5** k-shortest by `weight="length"` is approximate (RSA relies on it); **S6-7**
  `risk_groups` avoid-key also matches SRLG ids; **S6-8** best-effort overlap = fewest keys, not
  least risk; **S6-9** `oms_length_km` / forbidden-filter sync invariant.
- **S7-9** ref-mode QoT probe inheritance (retired if C7-7 lands first — check); **S7-10** runs
  within one placement assumed OMS-disjoint; **S7-11** `cost_facets` proxy semantics; **S7-12**
  single grid/spectrum threading invariant.
- **S3-2** `add_drop_osnr` 33 dB hardcode; **S3-3** flat NF polynomial; importer assumptions
  (addendum, incl. S3-add-4 decorative `Fiber.a_end/z_end` and S3-add-5 unread `num_spans`);
  Stage 5/6/7/8 assumption blocks recorded as module docstrings.

---

## PART 4 — PHASE 7 (last)

Execute `docs/plans/2026-06-07-phase-7-validate-commit-reconcile.md` as written, with these
adjustments accumulated from the fix batches (update the plan doc before starting):
1. **Task 1 partially pre-done** — `NetworkModel.clone()` exists from C3-1; keep the
   `diff_models` + `SnapshotStore.put` steps. `clone()` must respect the C3 freeze mechanics
   (always returns unfrozen) and must **not** copy `_optical_nodes` (removed in O3-1).
2. **Task 2's `simulate_ip_routing` rewrite** must preserve the C5-1 structured
   "no-QoT-recorded" state and the C5-5 `down_links` contract.
3. **Validator physics is now sound** — per-state recompute rides the C2 fixes (per-frequency
   probe, per-path comb, reverse-OMS direction) and C4-1 (failed assets survive recompute), and O1
   caching keeps K-state replay affordable.
4. `ProvisionLightpath` specs and `service_oms_sequence` consume travel-direction-correct
   routing from C6. **C6 landed this as `NewLightpathRun.src_node` / `.dst_node`** (S7-5): the
   `oms_sequence` stays in *physical-OMS order* (may be reverse-traversed), so provisioning must
   read direction from `src_node`/`dst_node`, **not** from `_lightpath_endpoints(oms_sequence)`
   (which reports physical endpoints, i.e. reversed for a return-direction run). The flat OMS
   solver is now directed (S6-4), so `compute_paths`/`compute_disjoint_paths` `oms_sequence`s are
   already travel-order-correct.
5. **Land Batch C9 (S7-13 layered-graph parallel-OMS collapse) before Phase 7 provisions
   restoration candidates over any multi-fiber topology** — else placement silently ignores all
   but one parallel fiber per wavelength.

---

## Verification (applies to every batch)

- `conda run -n multilayer-optical-mcp pytest` green before each commit; one commit per
  roadmap-finding fix (TDD: failing test → fix → pass, per repo plan convention).
- Physics batches (C1, C2, C7, C8, O1): re-pin `test_ground_truth_bridge.py` within `TOL_DB` and
  state in the commit message whether GSNR moved and why (e.g. TX-power fix moves it legitimately).
- Layer-consistency oracle: `tests/model/test_layer_consistency.py` +
  `test_injection_layer_consistency.py` after C3/C4/C5.
- O1: assert no orphaned `adv_nf_*` temp dirs after a bulk recompute.
- End-to-end after Phase 7: the plan doc's own `tests/test_server_phase7.py` six-tool flow, plus a
  make-before-break 3-state sequence exercising the transient check (CLAUDE.md risk item).

## Suggested first session

Batch C1 (pin bump) + C2 Step A — smallest high-value slice, each independently shippable.
