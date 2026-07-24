# Open TODOs — consolidated (2026-07-19)

> **Reconciled 2026-07-24 (latest pass) against HEAD `5555fc8`.** Verification pass over
> §6 (five parallel audits, one per cluster of claims): all 8 "deliberately left"
> simplifications reconfirmed **STILL TRUE** against current source (roll_off absence,
> fixed-ref_mode probing, amp band-edge top-slot drop, the 33dB ROADM add/drop constant,
> the flat NF polynomial, decorative `Fiber.a_end`/`z_end`, unread `edge["num_spans"]`,
> approximate length-weighted k-shortest, the `risk_groups`/SRLG avoid-key overlap,
> count-not-severity best-effort disjoint tie-break, the per-direction IP-capacity
> boundary, the `overflow_gbps` summing caveat — confirmed double-count-safe in practice
> — the OMS-disjoint-within-one-placement assumption, and the two independent
> `build_spectrum_state` call sites) — no drift, all still accurately described. One item
> was **not** a documented simplification but an open question ("status is unclear"), and
> it resolved to a real gap: **regen-node transponder-inventory gating is NOT
> implemented** — `validate_plan`/`commit_plan` have no transponder/inventory/regen
> check anywhere (`ViolationType` enum has no such variant; zero grep hits in
> `validate.py`/`commit.py`), and the only inventory gating that exists at all
> (`solve_allocation`'s `spare_inventory` packer, `allocation.py:336-353,466-488`) is a
> separate code path never wired to `compute_restoration`'s regen candidates. Moved from
> §6 to §5 as a confirmed standing gap (see below). Per user request, two items were
> dropped from this doc entirely rather than tracked further: the Stage 4 NLI fast-path
> (was §6, an intentional design deferral) and Suurballe/Bhandari guaranteed
> disjoint-pair routing (was §5, an intentional out-of-scope-for-this-inspection note).
> Neither represents new information — both are simply no longer tracked here. A
> follow-up plan, `docs/superpowers/plans/2026-07-24-section6-model-simplification-fixes.md`,
> now covers implementing fixes for the remaining §6 items (previously "documented
> limitations, not bugs" — reclassified as a backlog to close, not permanently accepted).

> **Reconciled 2026-07-24 (later same day) against HEAD `58bb792`.** The housekeeping flag
> below — `docs/inspection-roadmap.md` badly stale — is now **addressed**: a full update
> pass re-verified all ~60 numbered findings across all 8 stages against current source
> (five parallel audits), not just re-read. Verdict: the large majority are now RESOLVED
> in place with fixing-commit + file:line citations, including every `[High]`-severity
> finding (Stage 4's backward-OMS/probe-frequency bugs, Stage 5/6's non-total
> `simulate_ip_routing`/fake-disjoint-pair bug, Stage 8's failure/degradation
> non-composition bug). What remains open there is exclusively the low-priority/documented-
> simplification tail already tracked in §6 below, plus a couple of confirmed-intentional
> design choices. The doc was kept (not archived) as the detailed per-finding record;
> this consolidated doc remains the top-level summary.

> **Reconciled 2026-07-24 against HEAD `58bb792`.** §4's entire cleanup backlog —
> both the "DRY / layering polish" bullets and the "Test-coverage nits" — is now
> **RESOLVED** (`docs/superpowers/plans/2026-07-23-section4-cleanup-batch.md`,
> commits `adfb4c7`..`58bb792`, 10 tasks, subagent-driven-development with a
> task reviewer per task): a new neutral `model/placement_common.py` module
> holds `_forbidden_assets`/`_lever`/a shared `_status`/a shared
> `_harvest_placements`, which also resolved the `restoration.py`↔
> `route_service.py` import-cycle workaround (`restoration.py` now imports
> `route_service` at module scope, no more function-local deferral);
> `_harvest_alloc`'s `k=8` now reuses `_ROUTE_CAP`; `solve_allocation`'s dead
> `objective` param is removed (`solve_rsa`'s analogous one is untouched,
> explicitly out of scope); both "why-it's-safe" comments were added; all 5
> test-coverage nits were closed with new/extended tests, each independently
> re-verified against source by a task reviewer (not just re-run). Full suite
> **416 passed, 1 skipped** (406 baseline + 10 new/extended tests). See §4
> below for the itemized RESOLVED annotations.

> **Reconciled 2026-07-23 against HEAD `3097bc2`.** §3's small carried follow-up —
> making `FillPolicy.FULL` cache-friendly — is now **RESOLVED**
> (`docs/superpowers/plans/2026-07-23-full-policy-allcut-harvest.md`): a
> `harvest_qot` primitive propagates a full-grid comb once per (OMS, direction,
> mode) and extracts every slot's QoT from that single pass, memoized in a new
> content-addressed `HarvestCache`; `FillPolicy.FULL` is now the default
> everywhere ACTUAL used to be. german_17 E2E: **118s (ACTUAL, prior baseline) →
> 180s (FULL, new default)** — FULL is now affordable at build scale, though
> still somewhat costlier than ACTUAL per the dense-comb NLI cost, not a wash.
> One new documented model limitation surfaced along the way (§6) and one
> pre-existing, still-unexplained test failure was newly noticed (§4) — see below.

> **Reconciled 2026-07-20 against HEAD `9b35337`.** Latest pass: §2 scoring↔commit
> QoT convergence — the doc's highest-value correctness gap — is now **RESOLVED**
> (commit `9b35337`: post-actuation recompute seam on `commit_plan`'s live path +
> three guard tests). Earlier the same day: §3 QoT memoization marked **RESOLVED**
> (`QoTCache`, content-addressed, ~3.2× on the german_17 E2E), with a small
> FULL-policy comb/key follow-up carried; §7's stale "~47-minute" german_17 figure
> corrected to ~118s. **With §2 closed, no correctness gaps remain open — the
> backlog is now cleanup (§4), unscheduled features (§5), and documented
> simplifications (§6).**

Compiled by reading every `.md` in this repo (excluding `.pytest_cache/` and the
stale, non-registered `.claude/worktrees/*` snapshots — confirmed via
`git worktree list` that only the main worktree is live; the worktree copies are
pre-merge snapshots of docs already superseded in the main tree), every memory
entry in this project's auto-memory store, and every external plan file under
`~/.claude/plans/*.md`. Findings were cross-checked against `git log` and current
source where feasible, not just doc text — several docs (notably
`docs/inspection-roadmap.md`) claim far more open work than actually remains.

**Housekeeping flag — RESOLVED (2026-07-24).** `docs/inspection-roadmap.md` was badly
stale as of this writing (see the reconciliation banner now at the top of this doc for
the fix). Original text, for context: "Of its ~60 numbered findings, the large majority
are already fixed in code (Batches C1–C9, O1–O3 all landed per `git log`) but were never
annotated `RESOLVED` in the doc. Only a handful are genuinely still open (listed in §5
below)." That gap is now closed — every finding in that doc carries either a RESOLVED
annotation with fixing commit + file:line, or a confirmed-still-open note distinguishing
genuine gaps from documented simplifications.

---

## 1. In-flight right now

- **DONE (2026-07-20, commit `1041d4b`).** `pair_density` is now wired through
  `build_operating_network` (`model/scenario.py`): a pure pass-through in the
  convergence driver's `generate_demands` call, `None` default preserving the
  full-matrix behavior. Committed together with the pre-existing `generate_demands`
  implementation. Tested via a spy on `scenario.generate_demands` (a full build is
  ~minutes, so the forwarding test stubs the generator).

---

## 2. Highest-value correctness gap

- **Scoring↔commit QoT convergence** (`docs/superpowers/plans/2026-07-13-followups-and-next-steps.md`
  §2, reconfirmed live by the `two-graph-substrates-and-gaps` memory). `commit_plan`'s
  live path (`dry_run=False`) applies ops via `apply_op` but never recomputes QoT
  on the live/intended model — recompute only happens inside `validate_plan`'s
  discarded internal clone. Immediately after a real commit, a freshly-provisioned
  lightpath has no recorded QoT (its IP link reads unknown/down via
  `ip_link_capacity_gbps`'s `LookupError` path) until some later recompute fires —
  while candidate *scoring* seeds QoT immediately from the predicted GSNR.
  **Scoring and reality disagree on a freshly-committed lightpath until something
  else triggers a recompute**, and no test compares `score_candidate`/`route_service`
  against an actual `commit_plan` result. Two fix options were sketched in the
  followups doc: (a) have `commit_plan` recompute post-actuation, or (b) add the
  integration test + document the interim gap.
  - **RESOLVED (2026-07-20, commit `9b35337`).** Fix (a) shipped, per plan
    `docs/superpowers/plans/2026-07-20-commit-qot-convergence.md`: `commit_plan`'s
    live path now runs a best-effort post-actuation `recompute_if_possible` on the
    live model (`commit.py`), injected as a `recompute` seam mirroring the `actuator`
    param so QoT-free tests pass `recompute=lambda m, s: None`. Wrapped in try/except
    so a recompute failure never un-reports a committed plan (CLAUDE.md: typed, never
    raises). Both option-(b) tests also landed
    (`tests/model/test_commit_qot_convergence.py`): the live-commit regression (link
    no longer dark), a convergence guard (committed QoT equals an independent settle
    recompute and `evaluate_objective` counts that margin — asserted against a real
    recompute, not a predicted seed, so the predicted↔real comb gap doesn't make it
    brittle), and a seam guard. Full `tests/model` suite green (282 passed / 1
    skipped).

---

## 3. Performance

- **Enumeration cost center — RESOLVED (2026-07-20, commit `f0ee247`).** The
  layered graph's path enumeration (Yen's `shortest_simple_paths` against the
  wavelength-expanded graph) was the *dominant* routing cost, not a co-equal one:
  profiling one `solve_allocation_model` pass with a fake (instant) QoT took ~132s,
  98% inside Yen's. Cause: `build_layered_graph` created one wavelength vertex per
  spectrum slot (~48–96), so Yen's regenerated one near-duplicate path per free
  slot, all deduped away by `place_demands`' lam-ignoring key. Fixed with a
  WLin/WLout node-split (lets a segmented placement share one wavelength instead of
  being forced onto distinct slots — also a spectral-correctness fix) plus capping
  wavelength layers at the first globally-free slot. Same pass **132s → 0.13s
  (~1000×)**; full `tests/model` suite minutes → ~11s. `_RAW_PATH_CAP=1024` remains
  as a safety valve but no longer fires on lightly-loaded graphs.

- **QoT memoization for `compute_qot`/the real-adapter build path — RESOLVED
  (2026-07-20, commit `b8c0469`).** `QoTResultStore` remains the id-registry (uuid →
  breakdown, for later `get_qot_breakdown` retrieval); the memoization gap it did
  *not* cover is now closed by a separate `QoTCache` (`model/qot_results.py:43`) —
  content-addressed, off-model, injected like the result store. The key
  (`adapter.py:_cache_key`/`_path_physical_fingerprint`) fingerprints every
  GSNR-relevant input (resolved-path physical params via the frozen asset
  dataclasses + loading + direction + mode + probe freq), so an `inject_degradation`
  delta changes the key and misses automatically — no invalidation logic. Threaded
  through `make_adapter_evaluator`/`gated_qot`/`recompute_qot_under_loading`
  (default `None`; only the model-driven path is fingerprinted, so a raw
  topo/eqpt-file run is never cached). One shared cache across the german_17 E2E
  cut the build **377s → 118s (~3.2×, 61.8% hit rate, identical results)** — this
  was the last routing perf lever after the `f0ee247` enumeration fix.
  - **Follow-up — RESOLVED (2026-07-23, commits `16d4a02`..`3097bc2`).** Fix
    shipped per `docs/superpowers/plans/2026-07-23-full-policy-allcut-harvest.md`:
    `compute_qot`'s propagation body was extracted into a shared
    `_propagate_loading`/`_apply_penalties` core, reused by a new `harvest_qot`
    that propagates a full-grid comb **once** per (OMS path, direction, mode) and
    harvests every probe slot's GSNR/OSNR from that single pass, instead of one
    propagation per candidate probe frequency. Memoized in a new content-addressed
    `HarvestCache` (bounded LRU, keyed on `(oms_sequence, direction, mode_id,
    physical_fingerprint)` — no probe frequency, since one harvest answers every
    slot). `make_adapter_evaluator` now detects a full-grid `loading` and routes it
    through the harvest; any subset loading still falls through to `compute_qot`
    unchanged. With this in place, all ~11 acceptance-time `FillPolicy.ACTUAL`
    defaults were flipped to `FillPolicy.FULL` (`solve_rsa`, `solve_allocation`,
    `place_demands`, `build_operating_network`) — FULL is no longer the
    non-default it was described as here. The operating recompute
    (`recompute_qot_under_loading`/`_per_path_loading`/settle) was deliberately
    left untouched (still ACTUAL) — this was an acceptance-time-only change.
    german_17 E2E under the new FULL default: **180s**, down from a >39-minute
    *uncached* FULL baseline, and comparable to (somewhat above) the 118s ACTUAL
    figure recorded above — full `tests/model`/`tests/gnpy_adapter` green.

---

## 4. Cleanup backlog (all explicitly "Minor" in the source doc — batch into one PR)

From `docs/superpowers/plans/2026-07-13-followups-and-next-steps.md` §3–4:

**RESOLVED (2026-07-24), all of "DRY / layering polish"**
(`docs/superpowers/plans/2026-07-23-section4-cleanup-batch.md`, commits
`adfb4c7`..`3c832a1`, Tasks 1-5):
- Harvest + λ-free dedup logic duplicated between `route_service._harvest` and
  `allocation._harvest_alloc` — folded into `placement_common._harvest_placements`
  (Task 3, commit `9424a19`); both callers now delegate to it.
- Layering inversion (`_forbidden_assets`/`_lever` in `restoration.py`,
  `route_service.py` importing them, `compute_restoration`'s function-local
  deferred import of `route_service` to dodge the resulting cycle) — both
  helpers hoisted into a new neutral `model/placement_common.py` (Task 1,
  commit `adfb4c7`); `restoration.py`/`route_service.py`/`allocation.py`
  rewired onto it (Task 2, commit `eb10a4c`). The cycle is gone, not
  relocated: `restoration.py` now imports `route_service` at module scope,
  confirmed via a repo-wide grep that no back-reference remains anywhere in
  `model/`.
- `_status` reimplemented three ways — unified into
  `placement_common._status(has_any, fully_satisfied)` (Task 1/2). The merge
  preserves each caller's exact original edge-case behavior, including
  `allocation`'s vacuous-`SOLUTION`-on-zero-demands case, via a deliberate
  check-order (`fully_satisfied` before `has_any`) — verified by both an
  implementer and a task reviewer against all three original call sites.
- `_harvest_alloc` hardcodes `k=8` — now defaults to `_ROUTE_CAP` (Task 3).
- `solve_allocation`'s vestigial `objective: str` param — removed from
  `allocation.py`, `server.py`'s tool wrapper, and `CLAUDE.md`'s documented
  signature (Task 4, commit `0de53d0`). `solve_rsa`'s analogous unused param
  was left untouched — same underlying issue, but out of scope for this pass.
- `_stitch_ip_path` silent truncation — why-it's-safe comment added, tracing
  the actual downstream catch (`apply_op(RerouteService)` →
  `set_service_working_path` → `is_contiguous_path` → `ValueError` →
  `PlanError`) (Task 5, commit `3c832a1`).
- Per-demand `build_layered_graph(work)` rebuild — "intentional, not
  hoistable" comment added (Task 5).

**RESOLVED (2026-07-23):** `tests/test_server_phase7.py::test_commit_live_requires_confirm_then_reconcile_in_sync`
  root-caused and fixed in `commit.py`. Root cause: the 2026-07-20 QoT-convergence
  fix (`9b35337`) added a post-actuation `recompute(current, store_results)` call
  on the *live* model so a freshly-committed lightpath reports derived capacity
  immediately — but never mirrored that recompute onto `intended`, the snapshot
  captured earlier in `commit_plan` as `reconcile`'s comparison target. `qot_state`
  is derived state, not part of the plan's ops, so after every successful live
  commit `current._qot_state` gained an entry (`lpX`) that `intended._qot_state`
  never got, and `reconcile()` reported permanent phantom drift on the
  `qot_state` registry even though actuation succeeded perfectly. Confirmed via
  instrumented `diff_models(current, intended)`: `{'qot_state': {'removed':
  ('lpX',), ...}}`, with `current` holding a real `QoTState` and `intended`
  empty. Fix: run the same best-effort `recompute(intended, store_results)` on
  the intended clone before `store.put(intended)`, so both sides of the
  reconcile diff reflect identical post-recompute state. Full `tests/model` +
  `tests/test_server_phase7.py` + `tests/gnpy_adapter` green (365 passed, 1
  skipped); full `tests/` suite also green (406 passed, 1 skipped).

**RESOLVED (2026-07-24), all "Test-coverage nits"**
(`docs/superpowers/plans/2026-07-23-section4-cleanup-batch.md`, commits
`9234cfa`..`58bb792`, Tasks 6-10 — each ground-truth value independently
re-derived from source by a task reviewer, not just re-run):
- `evaluate_objective`'s snapshot-by-id branch — new test in
  `tests/test_server_phase8.py` proves `state=<id>` resolves via
  `snapshots.get()`, not `current()` (Task 6, commit `9234cfa`).
- `route_service_result_dict`'s nested `RoutePair` legs — the existing shape
  test now uses two genuinely distinct working/protection candidates instead
  of reusing one object, so it can actually catch a swapped-leg regression
  (Task 7, commit `e93e8dd`).
- `evaluate_objective`'s raw-vector test — all 7 terms now pinned to
  hand-derived ground truth, not just `transponders` and the scalar sign
  (Task 8, commit `df3cd1d`).
- `dropped_traffic`'s "disjoint sets ⇒ no double count" invariant — new
  regression fixture with one down link and one independently congested link,
  asserting the sum (130 = 80 dropped + 50 overflow) without double-counting
  either (Task 9, commit `8326ca7`).
- `disjoint_pairs`' `top_n > 2` truncation — new test confirms truncation
  keeps exactly the first `top_n` pairs in pairwise-scan generation order
  (Task 10, commit `58bb792`).

---

## 5. Standing feature gaps, never scheduled

- **`get_telemetry` (heatwave calibration).** Referenced across three docs
  (`CLAUDE-disaster.md`, `docs/service-level-routing-findings.md`, the
  route-service-and-evaluate-objective design spec) as "optional, not part of this
  work" — confirmed via grep: still not implemented anywhere in the repo.
- **Per-channel loading attribution** in `recompute_qot_under_loading` (which
  interferer drove which survivor's margin drop) — flagged deferred in the
  Phase 1–2 foundation plan; no evidence it was ever built.
- **Sensitivity tooling** (derive sensitivity by differencing `QoTBreakdown`s
  across branches) — same origin, same status: flagged, never built.
- **Regen-node transponder-inventory gating — CONFIRMED GAP (2026-07-24).** Moved
  here from §6, where it was carried as "status unclear." Verification pass found it
  is simply not built: the `multilayer-graph-restoration` design doc
  (`docs/plans/2026-06-14-multilayer-graph-restoration-design.md:73-75,164-166`)
  promises regen-node transponder availability is "checked at validate/commit
  (Phase 7)," but `validate_plan`/`commit_plan` (`model/validate.py`, `model/commit.py`)
  have no transponder/inventory/regen check at all — `ViolationType`'s full enum is
  `MODE_INFEASIBLE`, `SPECTRUM_CLASH`, `IP_LINK_OVERLOAD`, `DROPPED_TRAFFIC`,
  `DISJOINTNESS_COLLAPSE`, `PROTECTION_NOT_VIABLE`, `PROTECTION_OVERSUBSCRIBED`,
  `INVALID_PLAN`, with no inventory variant, and grep for
  `transponder|spare|inventory|regen` across both files is empty. The only inventory
  gating in the repo is `solve_allocation`'s `spare_inventory`-driven `_tp_need`/
  `_inv_ok`/`_dec_inv` in `model/allocation.py:336-353,466-488` — a separate heuristic
  packer, never called by `compute_restoration` or `validate_plan`. Net effect:
  `validate_plan` will pass a restoration plan that adds a new lightpath through a
  regen node with zero spare transponders. No test exercises this (`test_validate.py`,
  `test_validate_transient.py`, `test_commit_reconcile.py`,
  `test_commit_qot_convergence.py` all have zero `transponder|inventory|spare|regen`
  hits). Needs a new `ViolationType` (e.g. `REGEN_INVENTORY_EXHAUSTED`) plus a
  validate-time check against regen-node spare transponder counts, wired from
  `compute_restoration`'s regen candidates through to `validate_plan`.

---

## 6. Known, low-priority model simplifications (deliberately left as documented limitations, not bugs)

- `TransceiverMode` still has no `roll_off` field — the QoT probe's spectral shape
  uses a hardcoded 0.15 scalar regardless of mode (Stage 2 residual, confirmed
  still true in current `assets.py`).
- **Stage 7 finding 9, reconfirmed live:** `allocation._build_loading`/`place_demands`
  probe QoT only at a fixed `ref_mode`, assuming GSNR is mode-independent given a
  fixed probe shape — true today only because baud/roll-off isn't threaded from
  the delivered mode into the probe.
- A long tail of "resolved as documented limitation" items from the inspection
  roadmap (hardcoded 33dB ROADM add/drop penalty, flat NF polynomial in the
  advanced amp model, decorative/unused `Fiber.a_end`/`z_end`, unread
  `edge["num_spans"]`, approximate length-weighted k-shortest paths, `risk_groups`
  avoid-key naming overlap with SRLG ids, best-effort disjoint overlap minimizing
  shared-key *count* rather than severity, per-direction IP-capacity modeling
  boundary, `overflow_gbps`/dropped-demand summing caveat, the OMS-disjoint-within-
  one-placement assumption in hybrid placements, two independent `build_spectrum_state`
  calls that could drift). None of these are flagged as broken — they're conscious
  simplifications recorded so a future change doesn't reintroduce the underlying
  assumption unknowingly. **All 11 items in this bundle reconfirmed STILL TRUE
  against current source (2026-07-24 verification pass, HEAD `5555fc8`) — no drift.**
- **New (2026-07-23): amp band-edge always drops the top grid slot.** On this
  repo's `SpectrumGrid`/amp-band configuration, the topmost spectrum slot's
  occupied band (±50 GHz around center, for a 100 GHz-spaced grid) exceeds
  GNPy's `AMP_BAND` guard (only 25 GHz past the signal band) by 25 GHz, so every
  `Edfa` element silently demuxes that carrier out on *every* path, in *either*
  direction — a structural property of the grid/guard-band combination, not a
  per-topology quirk. Confirmed against GNPy's `is_in_band`/
  `demuxed_spectral_information` source, not just observed behavior. Surfaced by
  `harvest_qot` (§3 above), which now documents and tests around it (returns 47
  of 48 slots; consuming code must check membership, not index blindly). Not
  fixed at the root (would mean widening `AMP_BAND`'s guard past half a channel
  width, a GNPy-config-level change with broader implications) — recorded here
  as a documented limitation for whoever next touches spectrum-grid/amp-band
  config.

---

## 7. New e2e/integration test ideas (discussed this session, not yet spec'd)

Now that `build_operating_network` produces a realistically-loaded network
(instead of only 2–3 node toy fixtures):
- An integration test directly targeting §2's gap: real `commit_plan(dry_run=False)`
  + follow-up recompute, asserting post-commit `evaluate_objective` matches
  pre-commit `score_candidate` prediction.
- Reconcile/drift test against a *built* operating network (partial commit
  failure), not a hand-built fixture.
- What-if injection (`inject_degradation`/`inject_failure` + disjointness audit)
  at realistic scale against the built network's real gravity-shaped
  working/protection pairs.
- `route_service`/`evaluate_objective` regression against the built network's real
  grooming map and congestion.
- A fast CI-tier e2e: a small subgraph (6–8 nodes) with the real GNPy adapter, as
  a cheap alternative to the full german_17 build. (Note: after `f0ee247`
  enumeration + `b8c0469` QoT cache, that full build is now ~118s, not the ~47 min
  it once was — the CI-tier case is now about keeping *routine* CI cheap, not
  dodging a 47-minute wall.)

---

## Not on this list (checked and ruled out)

- `docs/service-level-routing-findings.md`'s gaps (flat-graph routing, no
  layered disjointness, missing `evaluate_objective`) — all resolved by the
  route_service/evaluate_objective merge (`ddc0fb4`).
- Every `docs/plans/*.md` phase plan (1–2, 3, 4, 5, 6a, 6b, 7) — all shipped;
  their "out of scope" sections are permanent design exclusions (control-plane
  signalling, weather/geo, physical-layer optimization, research-novelty claims),
  not deferred work.
- All ~10 external plan files under `~/.claude/plans/*.md` that relate to this
  repo — every one matches a shipped feature with corresponding commits and
  tests (verified via git log + source). Two files in that directory
  (`check-claude-md-and-plan-mighty-penguin.md`,
  `i-believe-you-ve-written-happy-unicorn.md`) belong to an unrelated project
  (a P4-switch ML thesis paper) and aren't part of this repo's TODO surface.
- Most memory entries are closed/historical (GNPy NF injection, power_mode
  decision, conda env, reverse-OMS fixtures, snapshot freeze contract,
  no-ROADM-less topologies, MultiDiGraph collapse-expand, disjointness endpoint
  fix, layered-graph OMS direction fix) — no open threads beyond what's captured
  above.
