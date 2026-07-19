# Stage 4 — Physical-Layer Propagation: Findings & Proposed Redesign

**Scope:** `src/multilayer_optical_mcp/gnpy_adapter/adapter.py`
(`compute_qot`, `gated_qot`, `recompute_qot_under_loading`), with supporting
context from `gnpy_adapter/translate.py`, `gnpy_adapter/synthesize.py`, and
`model/topology_import.py`.

**Tests reviewed:** `tests/gnpy_adapter/test_compute_qot.py`,
`test_per_direction.py`, `test_recompute_under_loading.py`, `test_toy_2route.py`.

This document records the inspection findings and the proposed Stage 4 redesign
agreed during review. **No code has been changed.** These are decisions and
action points to be scheduled.

---

## Part A — Confirmed findings (bugs / gaps)

### A1. Probe channel identified by `mode_id` only — frequency ignored *(high priority, correctness)*

`compute_qot` selects the carrier-under-test with
`next(c for c in loading.channels if c.mode_id == mode_id)` (`adapter.py:184`)
and has **no frequency parameter**. `recompute_qot_under_loading`
(`adapter.py:365-368`) passes `lp.mode_id` but **never `lp.center_freq_hz`**.

Consequence: in any realistic WDM load with several same-mode channels, *every*
same-mode lightpath is evaluated at the **first matching channel's frequency**;
each lightpath's actual `center_freq_hz` is silently ignored, and all same-mode
lightpaths return identical QoT pinned to one frequency. This directly
undermines the load-bearing `recompute_qot_under_loading` call.

### A2. Backward direction fabricated by reversing the forward OMS *(high priority, correctness)*

For `Direction.BACKWARD`, `compute_qot` reverses the forward OMS's element list
(`adapter.py:178-179`) instead of resolving the physically separate reverse OMS.

The importer (`topology_import.py:82-85`, `_add_directed_oms`) builds **two
independent directed OMS per link** (`oms_src_dst` and `oms_dst_src`), each with
its own amps/fibers (`amp_src_dst_*` vs `amp_dst_src_*`). The reverse chain is a
real, distinct object — that is what makes asymmetric per-direction degradation
representable.

Consequences:
- **Asymmetric degradation is invisible backward.** Raising NF on
  `amp_dst_src_0` is never seen by `compute_qot(BACKWARD)`, which walks
  `amp_src_dst_*` reversed. The model can express the asymmetry the CLAUDE.md
  contract promises; the adapter discards it.
- It is the root cause of A3 (ROADM skip).

The toy hides this: `_toy_model` defines only `oms-AZ` (no reverse OMS, no ROADM
at Z), so reversing the uid list is the only option there, and the pinned
ground-truth GSNR (fwd ~18.85 dB / bwd ~17.53 dB) validates a mechanism that is
wrong for importer topologies.

### A3. ROADM add/drop OSNR penalty dropped in the backward direction *(high priority, correctness)*

A GNPy `Roadm` propagates only for a registered `(from_degree, to_degree)` port
pairing. The synthesized graph registers only the **forward** pairing, so when
the reversed forward path hits a ROADM the `any(...)` guard
(`adapter.py:242-243`) fails, the ROADM is skipped, and it is excluded from
`_roadm_propagated`, so the post-propagation penalty loop (`adapter.py:290-292`)
never adds its `add_drop_osnr`. Backward GSNR is therefore optimistically biased.

**Roadmap wording correction:** the existing roadmap says "Forward GSNR is
therefore always lower than backward on multi-ROADM paths." This is **false** and
contradicted by the project's own ground truth (`test_per_direction.py:21`:
fwd ~18.85 dB **>** bwd ~17.53 dB). Backward is *optimistically biased* by the
omitted penalties; the net fwd-vs-bwd ordering depends on whether ASE asymmetry
or the dropped penalties dominate. The danger is in `gated_qot` (`adapter.py:351`,
returns the min): when backward is the physically worse direction, its inflated
GSNR can make the gate select an optimistic value and wrongly flip
`mode_feasible` to `True`.

A2 and A3 share one root cause and one fix: **propagate the paired reverse OMS
forward** rather than reversing the forward uid list.

### A4. Terminal drop-ROADM penalty dropped — in the *forward* direction too *(medium priority, correctness)*

Each OMS owns its **source** ROADM as `chain[0]` and treats the **destination**
ROADM as an endpoint (`synthesize.py:137-143`: `connect(src, chain[0])` is skipped
when `src == chain[0]`; the dst is wired as a trailing `connect(chain[-1], dst)`).
A transit ROADM reappears as the next OMS's `chain[0]`, so it is propagated once —
this convention correctly avoids double-counting transit ROADMs.

But the **final** drop ROADM of a path is nobody's `chain[0]`, so it is never in
any element list and never propagated. Its drop-side `add_drop_osnr` is omitted
**even forward**. A 0→1 lightpath applies `roadm_0`'s add penalty but not
`roadm_1`'s drop penalty. The toy hides this because its destination is `trx Z`,
not a ROADM.

### A5. Limiting-element diagnostic is effectively meaningless *(low priority, diagnostic quality)*

The limiting element is chosen as the minimum GSNR delta **including `-inf`**
(`adapter.py:304-315`). The first noise-introducing element (the booster) always
produces a `finite − inf = -inf` delta, so `limiting_element_id` is almost always
"the first amplifier," never the physically most-limiting span. The test
(`test_compute_qot.py:138`) only asserts it is a valid uid, masking this.

Proposed: exclude the `inf→finite` first-noise transition (consider only
finite-to-finite deltas), or define the limiting element by absolute GSNR drop in
the noise-loaded region.

### A6. Single-channel dummy injection — verified only on the toy; band-edge edge case *(low priority)*

`_ensure_min_two_channels` (`adapter.py:45-65`) injects a dummy carrier at
`probe_freq + 100 GHz` when the loading has exactly one channel, because a GNPy
EDFA needs ≥2 carriers to derive `slot_width`. The "<0.1 µdB cross-talk" claim in
the docstring is verified only on the 2-span toy. New edge case: if the probe
sits near the top of the SI band (synthesize uses 191.3–196.1 THz), the dummy at
`probe + 100 GHz` lands **outside** the band — a latent out-of-band-carrier bug.
Mitigation: place the dummy below the probe when near the upper edge (and see C2,
which makes the dummy rarely needed).

### A7. Minor / cross-references

- **`final_gsnr_db` vs `final_osnr_db` source mismatch** (`adapter.py:278-279`):
  GSNR is the last *finite* value; OSNR is unconditionally `snapshots[-1]`. They
  diverge only if the last element yields a non-finite GSNR (harmless in
  practice, chain ends on an amp). Document, no action.
- **`roll_off` hardcoded at 0.15** (`adapter.py:200`): same class as Stage 2
  point 4 (per-channel spectral shape). Fold into that fix.
- **`tx_power_dbm` default −20 dBm** in `build_si_for_loading`
  (`translate.py:110`): the TX-launch-power conflation already tracked as Stage 2
  point 3 / Stage 3 point 9. Applies to every adapter QoT call; cross-reference.
- **Per-call GNPy network rebuild** (`adapter.py:168-174`): `build_gnpy_network`
  runs on every `compute_qot`, i.e. 2N times in a bulk recompute — the Stage 3
  point 7 temp-dir/equipment cost, amplified here.

---

## Part B — Loading-model decision (resolved)

### B1. `LoadingState` is implicitly per-fiber/per-OMS, but used globally

A `LoadingState` is a flat tuple of `Channel`s with no fiber/path tag
(`translate.py:101-116`). Its `union` rejects any frequency overlap (Stage 2
point 1), which only makes sense **per fiber** — a real network reuses wavelengths
across disjoint fibers, which a single flat `LoadingState` cannot represent. So
the type is implicitly a single section's spectrum.

The over-inclusion of interferers comes from `recompute_qot_under_loading`
applying **one** global `LoadingState` to **every** path (`adapter.py:365-371`):
channels physically on other fibers are propagated as co-propagating interferers,
over-counting NLI (pessimistic, but imprecise).

### B2. Decision: model the actual per-OMS load; offer full-load as a derived query

Two options were weighed:

- **Full-load design assumption** (always compute worst-case full fill). Cheaper
  and a standard planning guarantee, and it bounds the make-before-break
  transient for free. **Rejected as the model** because it contradicts explicit
  CLAUDE.md commitments: survivor-QoT change during MBB becomes a no-op, the
  margin-feasibility gate goes static, `whatif_margin_threshold_sweep`'s "right
  now" collapses, and capacity-planning/defrag reuse suffers over-rejection.
- **Actual per-OMS load** (chosen). The only option consistent with the server's
  what-if + IP-coupling + MBB purpose, and likely cheaper than it looks because
  per-OMS spectrum occupancy already exists for RSA (`SpectrumGrid.slot_of`,
  `build_spectrum_state`, `check_spectrum_feasibility`, `solve_rsa`).

**Full-load remains available** as one constructible `LoadingState` (fill the
grid) for a worst-case design-margin check — a thin helper, not a separate engine,
with no contract contradiction.

### B3. Granularity is per-OMS, not per-span

Channels add/drop/express **only at ROADMs**, which are exactly OMS boundaries.
Within an OMS the multiplex is invariant across all spans. So occupancy attaches
to the **OMS** (aligning with `oms.elements` / `oms_sequence` and the RSA spectrum
state). A path's interferer set is a **sequence of per-OMS combs**; single-OMS
paths are exact, express (multi-OMS) paths genuinely differ per OMS.

---

## Part C — Proposed Stage 4 redesign (consolidated)

A single redesign subsumes A1–A4 and B and adds an efficient per-query engine.

### C1. Propagate per-OMS, reconstruct the comb at each ROADM boundary

Walk the path OMS-by-OMS in each OMS's **natural** direction (resolve the paired
reverse OMS for backward — fixes A2/A3, deletes the `reversed()` hack). At each
express ROADM boundary, rebuild the SI carrier set for the downstream OMS comb:

- **Keep** probe + express survivors: apply the ROADM (equalize to
  `target_pch_out_db`, add `add_drop_osnr`), preserve their accumulated
  `signal`/`ase`/`nli`.
- **Drop** carriers absent downstream (their already-imprinted upstream NLI on the
  probe correctly stays baked in).
- **Add** carriers new downstream: insert at ROADM target power, fresh `tx_osnr`
  noise, `nli = 0`.
- Reassemble per-carrier arrays in frequency order; continue with the next OMS's
  elements.

Walk **source through terminal ROADM** so the final drop penalty is applied
(fixes A4). The splice must construct the `SpectralInformation` arrays directly
(survivors carry noise; only adds get fresh init) — `create_arbitrary_spectral_information`
resets noise and cannot mix the two.

**Prerequisite:** classifying carriers as express/dropped/added needs **per-carrier
lightpath identity**, not just frequency (wavelength reuse means the same slot can
be a dropping lightpath on OMS_i and a fresh adding lightpath on OMS_{i+1}). Today
`Channel` has `mode_id` but no lightpath id; the comb representation (or the
spectrum state keyed `(OMS, slot) → lightpath_id`) must carry it.

### C2. Two engines sharing one per-OMS `SpectrumState`

Precompute per-OMS `SpectrumState` (committed comb: frequencies + per-channel
launch power + spectral shape), cached and invalidated on mutation. The probe
(channel-under-test) is inserted on top per OMS — this *is* `current ∪ {new}`
without provisioning, satisfying the arbitrary-loading contract.

- **Single-channel fast path** (per-service what-if, not-yet-committed probe):
  compute only the probe's NLI against the OMS backdrop. NLI for one channel is
  **O(N)** vs **O(N²)** for all channels; ASE is already per-channel. The probe
  must be inserted into the comb (its own SPM contributes to its NLI).
- **Full-comb path** (bulk `recompute_qot_under_loading`, and the **ground-truth
  oracle**): one propagation per OMS-path, read every carrier by frequency index
  (fixes A1 by construction; makes the dummy of A6 rarely needed). N single-channel
  passes and one full-comb pass are both O(N²) in bulk, so the full-comb path is
  preferred there.

### C3. Overload only the NLI extraction; gate against the oracle

- **Keep** GNPy's element `__call__` for power, gain, equalization, and ASE
  (stable public API; what ground-truth tests pin). Reuse GNPy's per-span launch
  powers (from the design pass) rather than re-deriving the flat backdrop, or the
  fast path drifts from the oracle.
- **Overload only** the per-channel NLI extraction (compute the GN/NLI term for
  the probe index given the full carrier arrays). This is the sole O(N) vs O(N²)
  win and the only internal worth touching.
- **Gate** it: assert single-channel NLI equals GNPy's `nli[probe_idx]` from a
  full-comb propagation within `TOL_DB` on the toy. The NLI/Raman solver is the
  GNPy internal flagged as numerically version-sensitive — keep the fast path in
  production but pin it against GNPy each release.

### C4. Remaining wrinkles

- The per-OMS backdrop is constant in **composition** but evolves in **power**
  within each span (fiber loss before the amp); the GN integral handles this via
  effective length. `SpectrumState` is therefore per-OMS arrays, not a scalar, and
  the probe's NLI depends on its frequency position in the comb.
- Single-channel EDFA `slot_width` (A6) is moot when the committed backdrop is
  populated; for a genuinely single-channel OMS, keep the dummy or compute the
  probe's EDFA gain/ASE directly.

---

## Part D — Test gaps to close alongside the fixes

1. **A1:** multiple same-mode channels at different frequencies through
   `recompute_qot_under_loading`, asserting each lightpath gets *its own*
   frequency's QoT.
2. **A2/A3:** a multi-OMS, multi-ROADM path comparing forward vs backward
   `add_drop_osnr` accounting; assert backward uses the reverse OMS's amps and
   that asymmetric NF on the reverse chain moves backward QoT.
3. **A4:** an importer-style path terminating at a ROADM; assert the terminal
   drop `add_drop_osnr` is applied.
4. **A5:** assert the limiting element is a noise-loaded-region element, not the
   first amplifier by construction.
5. **A6:** single-channel probe on a longer / band-edge path; assert the dummy
   stays in band and cross-talk is bounded.
6. **C2/C3:** single-channel fast-path NLI vs full-comb GNPy `nli[probe_idx]`
   within `TOL_DB` (the fast-path/oracle gate).
7. **Ground-truth re-pin:** any fixture migrated for the reverse-OMS / per-OMS
   change must re-pin GSNR within `TOL_DB` (the `test_ground_truth_bridge.py`
   discipline).

---

## Priority summary

| Item | Type | Priority |
|------|------|----------|
| A1 probe frequency | correctness | high |
| A2 reverse-OMS direction | correctness | high |
| A3 backward ROADM penalty | correctness | high |
| A4 terminal drop-ROADM (forward) | correctness | medium |
| B2/B3 per-OMS load model | architecture | high (enabler) |
| C1 per-OMS propagation + splice | redesign | high |
| C2 two-engine + SpectrumState | redesign/perf | medium |
| C3 NLI overload + oracle gate | perf | medium |
| A5 limiting element | diagnostic | low |
| A6 dummy injection band edge | robustness | low |
| A7 cross-references | cleanup | low |
