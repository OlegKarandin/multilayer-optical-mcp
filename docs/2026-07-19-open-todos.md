# Open TODOs — consolidated (2026-07-19)

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

**Housekeeping flag, before the list:** `docs/inspection-roadmap.md` is badly
stale. Of its ~60 numbered findings, the large majority are already fixed in code
(Batches C1–C9, O1–O3 all landed per `git log`) but were never annotated
`RESOLVED` in the doc. Only a handful are genuinely still open (listed in §5
below). Recommend either updating that doc's RESOLVED annotations in one pass, or
archiving it in favor of this consolidated list — as-is it will mislead anyone
(human or agent) who reads it at face value.

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
  - **Follow-up (small, open):** `FillPolicy.FULL`'s 48-carrier acceptance probe
    makes each cache *miss* a full-spectrum propagation, prohibitive at scale, so
    FULL is left non-default. Making the comb/key canonical per `(OMS, direction)`
    would let FULL benefit from the cache too. Named in the commit body; not yet
    spec'd. (ACTUAL policy, the default, is unaffected and fast.)

---

## 4. Cleanup backlog (all explicitly "Minor" in the source doc — batch into one PR)

From `docs/superpowers/plans/2026-07-13-followups-and-next-steps.md` §3–4:

**DRY / layering polish:**
- Harvest + λ-free dedup logic duplicated between `route_service._harvest` and
  `allocation._harvest_alloc` (same policies, same dedup key) — fold into one helper.
- Layering inversion: `_forbidden_assets`/`_lever` live in `restoration.py` but
  `route_service.py` imports them, forcing `compute_restoration` to import
  `route_service` via a function-local import to dodge a cycle — hoist both into
  a neutral module.
- `_status` reimplemented three ways (allocation / route_service / restoration)
  with identical semantics — share one helper.
- `_harvest_alloc` hardcodes `k=8` instead of reusing the module's `_ROUTE_CAP = 8`.
- `solve_allocation`'s `objective: str` param is vestigial (never read) — remove or wire.
- `_stitch_ip_path` silent truncation (`objective.py`) relies on a downstream
  `is_contiguous_path` raise to surface a broken walk — add a why-it's-safe
  comment or a typed guard.
- Per-demand `build_layered_graph(work)` rebuild in `solve_allocation` is correct
  (loading changes each iteration) but wants a one-line "intentional, not
  hoistable" comment.

**Test-coverage nits:**
- `evaluate_objective`'s snapshot-by-id branch (`snapshots.get(state)`) is untested.
- `route_service_result_dict`'s nested `RoutePair` leg contents asserted only implicitly.
- `evaluate_objective`'s raw-vector test under-verifies 5 of 7 terms numerically.
- `dropped_traffic`'s "disjoint sets ⇒ no double count" invariant has no dedicated
  regression test.
- `disjoint_pairs` has no `top_n > 2` truncation/tie-break test.

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
- **Suurballe/Bhandari guaranteed disjoint-pair routing** (Stage 6 design note) —
  explicitly named as "a future code change (its own PR), not part of this
  inspection." Today's disjoint-pair search is a heuristic, not a guaranteed-optimal
  algorithm, for the physical/link/node case.

---

## 6. Known, low-priority model simplifications (deliberately left as documented limitations, not bugs)

- `TransceiverMode` still has no `roll_off` field — the QoT probe's spectral shape
  uses a hardcoded 0.15 scalar regardless of mode (Stage 2 residual, confirmed
  still true in current `assets.py`).
- **Stage 7 finding 9, reconfirmed live:** `allocation._build_loading`/`place_demands`
  probe QoT only at a fixed `ref_mode`, assuming GSNR is mode-independent given a
  fixed probe shape — true today only because baud/roll-off isn't threaded from
  the delivered mode into the probe.
- Stage 4's C2/C3 NLI fast-path (two-engine single-channel path, overloading
  GNPy's NLI extraction) — **intentionally deferred by design decision**, not an
  oversight: "revisit only if post-O1 profiling shows NLI itself dominates."
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
  assumption unknowingly.
- Regen-node transponder-inventory gating status is unclear — the
  multilayer-graph-restoration design doc says availability is checked at
  validate/commit (Phase 7), but whether Phase 7's actual implementation added
  this specific check wasn't independently confirmed. Worth a quick verification
  pass rather than assuming either way.

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
