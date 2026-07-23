# FULL-policy all-CUT harvest — design

**Date:** 2026-07-23
**Status:** approved (brainstorm), pending implementation plan
**Goal:** make `FillPolicy.FULL` cheap enough to be the default acceptance policy,
by eliminating redundant whole-comb propagations, then flip the default to FULL.

---

## 1. Motivation & the profiling result that reframed it

`FillPolicy.FULL` probes each candidate against a fully-loaded 48-carrier comb so
the delivered mode stays feasible as the network fills (margin-stable,
order-independent — see `model/spectrum.py:FillPolicy`). It was left non-default
because a german_17 FULL build ran >39 min (killed). The prior hypothesis
(recorded in the `fill-policy-mfm-memoization` memory) was that each FULL cache
**miss** is ~O(N²) in carriers and therefore ~100s× costlier than ACTUAL's sparse
probe.

**Profiling on 2026-07-23 (real GNPy adapter, GNPy 2.14.0, `gn_model_analytic`,
2-span `toy_2span` forward OMS) refuted that:**

- A single FULL (48-carrier) propagation is **1.31×** an ACTUAL (1-carrier) one
  (2.04 vs 1.56 ms/call).
- cProfile hotspot is **carrier-count-independent**: EDFA `interpol_params` /
  `_gain_profile` (polyfit/lstsq/`get_impairment`, ~40%) + fiber attenuation/SRS
  (~30%). `compute_nli`/`_gn_analytic`/`_psi` — the only O(N²) piece — is **~18%**.
- An N-sweep (N=1..48) is flat: run-to-run jitter exceeds the N-trend.

So the >39-min build blowup is **not** per-propagation NLI cost. It is
**per-OMS redundancy**: today the acceptance path runs a *distinct* full-comb
propagation for *every probe slot* on an OMS as the network fills (up to ~48 per
OMS × direction), plus cache-key fragmentation that prevents reuse across probe
slots.

**Decisively out of scope as a result:** caching the power-independent `eta`
matrix, computing only the CUT row, and the "overload GNPy's NLI extraction"
fast-path. All three attack an 18% slice of a per-call cost that is already only
1.3× ACTUAL. They are not worth the GNPy-internal surgery.

### The key enabler

GNPy's `Fiber.propagate` already does `spectral_info.add_nli(nli)` for **all**
carriers in one pass; `_extract_gsnr_osnr(si, idx)` reads only `probe_idx`,
discarding the other 47 answers. One propagation of the canonical full comb
already contains the GSNR for **every** probe slot. The fix is to stop throwing
those away.

---

## 2. Chosen approach

**Approach A — harvest evaluator + dedicated harvest cache, with implicit
full-comb detection.** (Approaches B "bulk-prefill `QoTCache`" and C "push all-CUT
into `compute_qot`" were rejected: B couples brittly to `_cache_key` internals;
C muddies `compute_qot`'s single-probe contract.)

Physics is unchanged — we read the all-channel `si.nli`/`si.ase` GNPy already
computes. Only the **acceptance** evaluator is touched; the operating recompute
is untouched.

**Detection is implicit:** the acceptance path sets `fill_policy`, but the
`QotEvaluator` receives only `loading`. A FULL loading *is* the full grid, so
"loading spans all grid slots" is an exact, safe trigger. If an ACTUAL build ever
genuinely fills 48/48 on an OMS, harvesting returns the identical answer — so the
trigger is correct in both cases and needs **zero new plumbing through the
solvers**. Flipping the default to FULL then routes through the harvest
automatically.

---

## 3. Components

### 3.1 `_propagate_loading` — shared propagation core (refactor, `adapter.py`)

Extract the propagation+penalty body of `compute_qot` into a private helper:

```
_propagate_loading(model, oms_sequence, direction, loading, mode)
    -> (si, uids, elements, roadm_propagated, baud_rate)
```

`compute_qot` becomes: `_propagate_loading` → extract at `probe_idx` → assemble
one `QoTState` + `QoTBreakdown` (unchanged public behavior). Pure refactor, no
behavior change. This is what makes the parity guarantee (§6) structural rather
than coincidental.

### 3.2 `harvest_qot` — one propagation, all slots (`adapter.py`)

```
harvest_qot(model, oms_sequence, direction, mode_id, full_comb) -> dict[int, QoTState]
```

- Frequency-sorts the full-grid comb so SI index *i* ↔ grid slot *i*; calls
  `_propagate_loading` **once**.
- For every carrier index *i*: `_extract_gsnr_osnr(si, i)` + the per-channel
  post-propagation penalty (`si.tx_osnr[i]` plus the constant per-propagated-ROADM
  `add_drop_osnr`) → a `QoTState` whose `margin`/`mode_feasible` are computed vs
  the passed `mode_id`, mirroring `compute_qot`.
- Returns `{slot: QoTState}`. **No `QoTResultStore` writes and no per-slot
  breakdowns** — the acceptance evaluator discards `result_id`, so generating 48
  breakdowns is pure waste and is skipped.

### 3.3 `HarvestCache` — coarse, probe-free cache (`model/qot_results.py`)

Mirrors `QoTCache` (bounded LRU, off-model, injected). Value is the whole
`{slot: QoTState}` vector for one propagation.

**Key:**
```
(tuple(oms_sequence), direction.value, mode_id,
 _path_physical_fingerprint(model, oms_sequence, direction))
```

No `center_freq_hz`, no `loading.channels` — that is the point: every probe slot
on a path collapses to one entry. The comb geometry is fixed by the full-grid
trigger (§2), so it is not part of the key; this assumption is recorded here so a
future change that triggers harvest on a *partial* comb must revisit the key.
Reuses `_path_physical_fingerprint`, so an `inject_degradation` NF/loss delta
flips the key automatically — content-addressed, no explicit invalidation, same
discipline as `QoTCache`.

### 3.4 Detection in `make_adapter_evaluator` (`model/allocation.py`)

Gains an optional `harvest_cache=None`. Inside `_eval(oms_sequence, direction,
mode_id, loading)`:

1. Compute the loading's slot set via `SpectrumGrid.default().slot_of`.
2. **If it spans all `num_slots`** and `harvest_cache is not None` → harvest
   branch: build the key; on miss call `harvest_qot` and store the vector; return
   `vector[probe_slot]`, where `probe_slot` is the slot of the channel
   `compute_qot` would select as probe (first channel matching `mode_id` =
   `channels[0]` under FULL).
3. **Else** → today's exact `compute_qot(..., cache=qot_cache)` path,
   byte-identical.

Safe-degrade: `harvest_cache is None` skips the branch (FULL still works,
uncached). An off-grid channel makes `slot_of` raise → caught → normal path.
ACTUAL subset combs never trigger harvest.

---

## 4. The default flip

Per the approved "harvest + flip default" scope:

- Flip the ~10 `fill_policy: FillPolicy = FillPolicy.ACTUAL` defaults → `FULL`
  across the allocation solvers (`allocation.py`) and
  `scenario.build_operating_network` (`scenario.py`).
- Wire a shared `HarvestCache` at the same construction sites that build the
  shared `QoTCache` today (`scenario.py`, `server.py`), passed into
  `make_adapter_evaluator(model, store, cache=..., harvest_cache=...)`.

**Operating recompute stays ACTUAL and untouched.** `recompute_qot_under_loading`,
`_per_path_loading`, and the scenario settle keep their ACTUAL loading. Only the
acceptance evaluator harvests. Rationale (from the `fill-policy-mfm-memoization`
memory): GSNR is monotonic in interferer count, so a FULL-accepted mode is
guaranteed feasible under any lighter real load; threading FULL into the operating
recompute would report pessimistic margins that mismatch reality and could wrongly
gate a healthy link to capacity 0 (a confident-wrong-number bug).

---

## 5. Test-migration policy

Per the approved "pin ACTUAL tests explicitly" choice:

- Existing tests that assert ACTUAL-specific mode picks get an explicit
  `fill_policy=FillPolicy.ACTUAL` so they keep verifying what they were written to
  check.
- New tests cover the flipped FULL default.
- 8 test files touch fill-policy or mode assertions today
  (`test_scenario`, `test_fill_policy`, `test_allocation`, `test_plan`,
  `test_rsa`, `test_views`, `test_server_phase7`, `test_loading`) — each failing
  assertion after the flip is pinned to ACTUAL if it is ACTUAL-specific, or given a
  new FULL expectation if it should track the default.

---

## 6. Testing

1. **Parity (core):** on `toy_2span`, `harvest_qot(...)[j]` equals
   `compute_qot(probe=slot j)` for every `j`, both directions, within float
   tolerance. (Near-tautological given the shared `_propagate_loading`, but guards
   the index→slot mapping and penalty indexing.)
2. **Cache correctness:** fingerprint completeness — mutate each GSNR-relevant
   input (fiber loss, amp NF, a mode field, direction) and assert a `HarvestCache`
   miss; assert identical results cached vs uncached.
3. **Detection:** a full-grid loading harvests (one propagation, asserted via a
   propagation spy/counter); a subset loading does not.
4. **Perf smoke:** N distinct probe slots on one OMS ⇒ exactly one propagation
   (spy count), asserting the ~48×→1 collapse.
5. **ACTUAL pinned:** existing ACTUAL-asserting tests, with explicit
   `fill_policy=FillPolicy.ACTUAL`, stay green.
6. **New FULL-default tests:** `build_operating_network` / `solve_rsa` /
   `solve_allocation` now default to FULL — assert the margin-stable mode picks.
7. **german_17 E2E:** the FULL build now completes; record the new wall-time and
   assert results match a FULL reference.

All tests are LLM-free and deterministic (repo testing discipline).

---

## 7. Edge cases & open risks

- **german_17 per-call cost is unmeasured** (multi-span OMSes add more fibers).
  The design is correct regardless; the implementation plan includes profiling one
  real long OMS before blessing the E2E timing target. Expectation: the ratio
  stays ~1.3× (more fibers add NLI *and* N-independent amp cost proportionally),
  but this is to be confirmed, not assumed.
- **Probe identification** relies on `channels[0]` being the probe under FULL
  (guaranteed by `_build_loading`, which prepends the probe). The harvest re-sorts
  internally, so ordering only affects *which slot's* `QoTState` is returned —
  specified explicitly, not left implicit.
- **`_ensure_min_two_channels`** is a no-op for a 48-carrier comb, so there is no
  dummy-channel index skew in the harvest.
- **Partial-comb harvest** is explicitly *not* supported by the current key (§3.3);
  a future change enabling it must add the comb geometry to the key.

---

## 8. Out of scope

- `eta`-matrix caching, CUT-row-only NLI, GNPy-internal NLI-extraction overload
  (§1 — attacks the wrong 18%).
- Any change to the operating recompute / settle path (§4).
- Physical-layer optimization, weather/geo, control-plane signalling (permanent
  repo exclusions).
