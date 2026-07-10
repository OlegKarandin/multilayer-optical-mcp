# Code Inspection Roadmap

Covers the full stack: network loading → physical layer → routing → restoration.
Focus is on **assumptions**, not line-by-line correctness.

---

## Stage 1 — Data model foundation

**Files:** `src/.../model/assets.py`, `src/.../model/network.py`
**Tests:** `tests/model/test_assets.py`, `tests/model/test_network.py`

1. `RiskGroup.metadata: dict` inside `frozen=True` — the field can't be reassigned but the dict is mutable. Anyone holding the object can mutate it. Consider `MappingProxyType`.
2. `ip_link_capacity_gbps` (network.py:223) raises `LookupError` when QoT state is absent — does **not** return 0.0. Every caller must have run `recompute_qot_under_loading` first or gets an exception.
3. `OpticalNode.kind` is an unconstrained `str`. Values `"roadm"`, `"amplifier_site"`, `"transceiver_site"` are documented but never enforced.
4. `add_lightpath` validates OMS existence and mode existence, but **does not validate OMS chaining** (each OMS's `dst_node_id` must equal the next OMS's `src_node_id`). A gap or inversion passes silently and surfaces at propagation time. **ACTION: add a chaining check in `NetworkModel.add_lightpath` — iterate the OMS sequence and assert `oms[i].dst_node_id == oms[i+1].src_node_id`.**
5. `Lightpath.center_freq_hz` is not validated against the spectrum grid at add-time. `SpectrumGrid.slot_of()` has the machinery; the right enforcement point is `add_lightpath`. Requires `NetworkModel` to hold a `SpectrumGrid` reference. Currently the error only surfaces at `build_spectrum_state` (routing time). **ACTION: give `NetworkModel` an optional `grid: SpectrumGrid` and call `grid.slot_of(lp.center_freq_hz)` in `add_lightpath`.**
6. `_optical_nodes` is written by `topology_import.py` and cloned by snapshots but never read anywhere. Every routing/topology function uses `oms.src_node_id` / `oms.dst_node_id` as plain strings. **ACTION: remove `_optical_nodes` and `add_optical_node`; remove the corresponding line in `topology_import.py` and `snapshots.py`.**
7. `set_lightpath_mode`, `apply_nf_delta`, and `apply_loss_delta` mutate physics without invalidating `_qot_state`. After any of these, `ip_link_capacity_gbps` silently returns a stale value. **ACTION: clear the relevant QoT entry on mode change; clear all QoT entries on NF/loss delta (any lightpath crossing the mutated element is affected).**
8. `set_service_working_path` does not check whether links are up (margin ≥ 0). A service can be routed over a down link without error. This is intentional for pre-planning restoration paths before lightpaths are provisioned — enforcing link-up status here would break make-before-break workflows. **Note: document explicitly that `set_service_working_path` is a statement of intended routing; `simulate_ip_routing` is the place that reports actual drops.**
9. `SnapshotStore.get()` returns the live stored `NetworkModel`, not a clone. Callers can mutate stored snapshots directly, silently corrupting them for all future `branch()` and `restore()` calls. **ACTION: add a `_frozen` flag to `NetworkModel`; `_check_mutable()` guard on every mutating method; `get()` returns `self._clone(snapshot).freeze()`. `_clone()` always produces an unfrozen copy. `branch()` continues to return an unfrozen working copy via `_current`.**

---

## Stage 2 — Loading state abstraction

**Files:** `src/.../gnpy_adapter/loading.py`, `src/.../gnpy_adapter/translate.py`
**Tests:** `tests/gnpy_adapter/test_loading.py`, `tests/gnpy_adapter/test_translate.py`

1. `LoadingState.union` raises on spectrum overlap — but also on self-overlap. The overlap predicate uses strict `<` on both sides, so channels that are adjacent (touching but not overlapping, i.e. `a.high_hz == b.low_hz`) are correctly allowed. Self-union on a non-empty state raises — also correct, as the same channel cannot occupy the same slot twice. **No code fix needed. ACTION: add `test_union_allows_adjacent_channels` to `test_loading.py` to pin this invariant explicitly — a future change relaxing `<` to `<=` would silently break 50 GHz-spaced grids.**

> **RESOLVED — Batch C7 (2026-07-09).** Stage 2 points 2 and 4 are fixed. Point 2:
> `Channel.power_dbm` is now `Optional[float]` (None = default, 0.0 = literal 0 dBm);
> sentinel check is `is not None`; all 18 test callsites + internal callsites migrated;
> GSNR unchanged. Point 4: `load_modulation_formats` reads `symbol_rate_gbaud` per-format
> (file-level value is the default fallback), and `Channel` carries optional
> `baud_rate_hz`/`roll_off` consumed per-carrier by `build_si_for_loading`
> (`loading_from_model` populates baud from each lightpath's mode). All current formats
> share 87.5 Gbaud, so ground truth is unchanged. *`roll_off` has no per-mode source yet
> (`TransceiverMode` has no roll-off field), so it falls back to the scalar 0.15 until a
> source is added — noted as a residual.*

2. `Channel.power_dbm = 0.0` is the sentinel meaning "use `tx_power_dbm` default" in `build_si_for_loading` (`translate.py:105`). There is no `None` option. A legitimately 0 dBm channel (1 mW, a common coherent launch power) cannot be expressed. **ACTION: change `power_dbm: float` to `power_dbm: Optional[float]` in `loading.py`; update the sentinel check in `translate.py` from `!= 0.0` to `is not None`; update the docstring on line 75 of `translate.py`; migrate all 18 callsites in `tests/` that use `0.0` as "don't care" to `None` — affected files: `test_loading.py`, `test_translate.py`, `test_compute_qot.py`, `test_per_direction.py`, `test_recompute_under_loading.py`, `test_toy_2route.py`, `test_ground_truth_bridge.py`, `test_whatif.py`; and the two src callsites in `allocation.py` lines 106–108 and `whatif.py` line 36.**

3. `tx_power_w` in `build_si_for_loading` (`translate.py`) is always built as `np.full(n, tx_power_dbm_default)` where the default is -20 dBm. -20 dBm is the ROADM output power (the `target_pch_out_db` the ROADM normalises to before the booster). GNPy's `tx_power` parameter represents the **transponder launch power** — what the TX port emits before the ROADM — which is typically around 0 dBm for a coherent pluggable (this is the OpenROADM IFR reference). Using -20 dBm here means the transmitter noise floor (noise_tx = tx_power / tx_osnr_linear) is computed 20 dB too low, making the tx OSNR budget contribution 20 dB too optimistic. **ACTION: introduce a separate `tx_launch_power_dbm` parameter in `build_si_for_loading` (distinct from `tx_power_dbm` which controls the pch default) with a default of 0 dBm (standard transponder reference); build `tx_power_w` from this value, not from the ROADM pch default. The two concepts — fiber-input channel power and transponder launch power — must be kept separate.**

4. `baud_rate` and `roll_off` are broadcast as scalars to every carrier in `build_si_for_loading` (`translate.py:111–112`). The root is in `modulation_formats.yaml`: `symbol_rate_gbaud` is a **file-level global** (not a per-format field), and `load_modulation_formats` in `modes.py` copies that single value into every `TransceiverMode.symbol_rate_baud`. Future formats (1.2T, 1.6T) will run at different baud rates, and a loaded `LoadingState` mixing, say, 400G (87.5 Gbaud) with a 1.2T (140 Gbaud) channel will silently compute NLI with the wrong spectral shape for one of them. **ACTION (two steps): (a) move `symbol_rate_gbaud` inside each format entry in `modulation_formats.yaml` and update `load_modulation_formats` to read it per-format; (b) add `baud_rate_hz: float` and `roll_off: float` to `Channel` in `loading.py`; update all Channel construction callsites to populate these from the relevant `TransceiverMode`; update `build_si_for_loading` to use per-channel arrays instead of broadcast scalars. The `baud_rate` / `roll_off` scalar parameters on `build_si_for_loading` can then be removed or kept as fallback defaults.**

---

## Stage 3 — Synthesize: NetworkModel → GNPy

**File:** `src/.../gnpy_adapter/synthesize.py`
**Tests:** `tests/gnpy_adapter/test_synthesize.py`, `tests/gnpy_adapter/test_ground_truth_bridge.py`

> **RESOLVED — Batch C7 (2026-07-09/10).** Points 1, 4, 5, 6, 11 fixed. **Point 11 (drop the
> `_synthetic_trx` branch) landed via Option B** — see the RESOLVED note appended to point 11;
> point 4 (mistyped ROADM silently demoted) is fixed for free by point 11's `else raise`.
> Point 1: `model_to_gnpy_equipment` emits one GNPy `Fiber` per registered `FiberType`
> (dispersion/effective_area/pmd from the model); `FiberType.gamma` (declared but never
> read) was renamed to `effective_area` — what synthesis actually feeds GNPy — default
> 83e-12 so SSMF ground truth is unchanged. Point 5: per-ROADM `target_pch_out_db` is
> emitted in the topology element's `params`. Point 6: `Amplifier.tilt_db` is emitted as
> `operational.tilt_target`. Both verified to reach GNPy through the full design pass;
> GSNR unchanged for the toy/german cases whose values equal the former hardcodes.

1. **Only SSMF exported + per-fiber-type physics dropped.** `model_to_gnpy_equipment` hardcodes one equipment `Fiber` entry `"SSMF"` (synthesize.py:60–61). The topology element keeps `type_variety: f.type_variety` (synthesize.py:90) but exports **only** `loss_coef` from the model's `FiberType`; `dispersion`, `gamma`/`effective_area`, and `pmd_coef` (all real fields on `FiberType`, assets.py:19–24) never reach GNPy. Two consequences: (a) a second variety (LEAF, NZDSF) likely raises `KeyError` in `network_from_json` — *not* a silent SSMF substitution as previously worded; (b) even a correctly-typed SSMF fiber with a non-default dispersion/γ in the model silently runs on library defaults. **ACTION: emit one equipment `Fiber` entry per registered `FiberType`, populating dispersion/effective-area/pmd from the model, and pin GSNR doesn't change for the existing SSMF case.**
2. **ROADM `add_drop_osnr` hardcoded to 33 dB.** The `ROADM` dataclass has no such field; synthesis always emits 33 dB regardless of hardware.
3. **NF polynomial is flat.** `nf_fit_coeff: [0.0, 0.0, 0.0, nf]` — constant NF independent of gain. Valid for the advanced_model requirement (CLAUDE.md) but a simplification for real gain-dependent NF amps.
4. **`_resolve_endpoint` creates synthetic Transceivers** for any OMS endpoint that isn't a named ROADM or registered transceiver. A mis-named site ID silently produces a synthetic Transceiver instead of a ROADM, dropping the add_drop_osnr penalty for that site.
5. **`ROADM.target_pch_out_db` is dropped (field exists, unlike point 2).** The `ROADM` dataclass has a per-instance `target_pch_out_db` (assets.py:49, default −20), but `model_to_gnpy_topology` emits ROADM elements as bare `{uid, type}` (synthesize.py:80–81) and the equipment `Roadm` entry hardcodes `ROADM_TARGET_PCH_OUT_DB = -20` (synthesize.py:10/66). A ROADM configured with a non-default per-channel output power is silently overridden. **ACTION: emit per-ROADM `target_pch_out_db` in the topology element (or per-variety `Roadm` equipment entries) instead of the global constant.**
6. **`Amplifier.tilt_db` is dropped.** Topology emits `"tilt_target": 0` hardcoded (synthesize.py:87) while the `Amplifier` dataclass carries `tilt_db` (assets.py:43). Any non-zero per-amp tilt in the model is ignored. **ACTION: pass `a.tilt_db` into `operational.tilt_target`.**
7. **Temp-directory leak in the load-bearing path.** `model_to_gnpy_equipment` calls `tempfile.mkdtemp()` on every invocation when `_tmpdir is None` (synthesize.py:42–43) and never cleans up; `_equipment_from_dict` may `mkdtemp()` again (synthesize.py:210). `build_gnpy_network` runs on every `compute_qot` / `recompute_qot_under_loading` call, so a long what-if session accumulates orphaned temp dirs (`adv_nf_*.json` + `eqpt.json`) on disk — the disk analogue of the unbounded-branch footgun in CLAUDE.md. **ACTION: cache synthesized equipment per model-fingerprint, or use a context-managed temp dir tied to the adapter lifecycle.**
8. **Hardcoded amp envelope (low priority).** Every Edfa gets `gain_flatmax: 25, gain_min: 0, p_max: 23` (synthesize.py:49–51). An OMS whose model amp specifies `gain_db > 25` is silently clamped/redesigned by GNPy's design pass. **ACTION: document the single-envelope assumption, or derive these bounds from the model amps.**
9. **`power == tx_power` in the design reference channel.** `gnpy_design_network` sets both `power` and `tx_power` to `dbm2watt(si.power_dbm)` in the `PathRequest` (synthesize.py:179/182). GNPy treats these as distinct: `power` is the per-channel design reference, `tx_power` is the transponder launch power that feeds the TX OSNR noise floor (`noise_tx = tx_power / tx_osnr_linear`). This is the **same conflation as Stage 2 point 3** (`translate.py` `tx_power_w`), now in the design path — here `si.power_dbm` is 0 dBm so it is less wrong than the −20 dBm case, but the two concepts still share one value. **ACTION: when introducing `tx_launch_power_dbm` (Stage 2 point 3), apply it here too so the design reference and per-direction propagation agree on the TX noise floor.**
10. **Three hand-tuned frequency bands invite drift.** Three subtly different ranges are written as literals: amp advanced config 191.275–196.125 THz (synthesize.py:24), SI 191.3–196.1 THz (line 69), Transceiver tunability 191.35–196.1 THz (line 73). They are *intentionally* different (amp band ⊇ SI band ⊇ a transceiver tuning subset), so this is not pure duplication — but the ⊇ ordering and the magic 25/50 GHz guard offsets are implicit and can silently drift out of order on edit. **ACTION: introduce a `Band(f_min, f_max)` value object with explicit guard-margin derivation (e.g. `.with_guard(±25 GHz)` for the amp band) so the relationship is encoded, not three independent literals. Not a flat deduplication — preserve the three distinct bands.**
11. **`model_to_gnpy_topology` straddles two endpoint conventions (cleanup).** The function supports both importer-built models (ROADM embedded as `chain[0]`, guarded by `if src_uid != chain[0]` at synthesize.py:138) and legacy/test models (bare unregistered transceiver UIDs in `src_node_id`/`dst_node_id`, synthesized via the `_resolve_endpoint` third branch + deferred `_synthetic_trx` flush at lines 145–147). The synthetic-transceiver fallback is also what masks Stage 3 point 4 (a mistyped ROADM site becomes a Transceiver instead of erroring). **ACTION: investigate dropping legacy-convention support — see the investigation note below.**

### Investigation note (point 11): dropping legacy-convention support

**Finding — the legacy convention is test-only, but load-bearing for the core adapter tests.** Production builds models exclusively via `model_from_abstract_graph` (topology_import.py), which names ROADMs `roadm_<id>` and resolves OMS endpoints to them, registering real `trx_<id>` transceivers — it **never** hits the `_synthetic_trx` branch. The synthetic-transceiver fallback is exercised only by adapter test fixtures that use a non-`roadm_` ROADM name plus bare unregistered endpoint UIDs, and that go through synthesis (no `topo_path`):

| Test fixture | Hits synthetic branch? | Exercises synthesis? |
|---|---|---|
| `test_compute_qot.py::_toy_model` (`"ROADM A"` + `"trx A"`/`"trx Z"`) | yes | yes (no `topo_path`) |
| `test_per_direction.py` (reuses `_toy_model`) | yes | yes |
| `test_translate.py` (`"ROADM A"` + `"trx A"`/`"trx Z"`) | yes | yes |
| `test_ground_truth_bridge.py` (`_toy_model_synthesized`/`_toy_model_legacy`) | yes | `_synthesized` yes; `_legacy` uses `topo_path` (no) |
| `test_toy_2route.py` | n/a | **no** — uses `topo_path=TOPO_2ROUTE` → `load_toy`, never calls `model_to_gnpy_topology` |
| model/solver tests (`test_rsa`, `test_restoration`, `test_avoidance`, …) | n/a | no — never call `compute_qot`/synthesis |

**Verdict: feasible and worthwhile, as a self-contained test-refactor PR.**
- *Simplification:* delete `_synthetic_trx` (line 100), the third branch of `_resolve_endpoint` (lines 121–123), and the deferred flush (lines 145–147). `_resolve_endpoint` collapses to `roadm_<id>` → registered transceiver → **else raise**. The `if src_uid != chain[0]` guard (line 138) **stays** (it serves the importer convention, which is kept).
- *Correctness bonus:* the `else raise` turns Stage 3 point 4 (mistyped ROADM site silently demoted to a Transceiver, losing add/drop OSNR) into a loud error — fixes point 4 for free.
- *Cost:* migrate ~4 adapter test fixtures to the importer convention (rename ROADMs to `roadm_<id>`, register a `trx_<id>`, let OMS endpoints resolve to ROADMs). Because this touches `test_ground_truth_bridge.py`, each migrated fixture must be re-pinned to confirm GSNR is unchanged within `TOL_DB` — that test is the gate for the PR.

### Point 11 — RESOLVED via Option B (2026-07-10)

**Landed.** Plan: `docs/plans/2026-07-10-s3-11-symmetrize-toy-drop-synthetic-trx.md`. The toy line
system was **symmetrized** — a drop `ROADM Z` was added to `toy_2span.json` and
`_toy_model_synthesized` (preamp → ROADM Z → trx Z), and the three synthesis fixtures that used
bare-`trx` OMS endpoints (`_toy_model_synthesized`, `test_compute_qot._toy_model`,
`test_server_phase4_rsa._seed_app`) were migrated to the importer convention (`roadm_<id>` add/drop
ROADMs, registered `trx_<id>`, node-id OMS endpoints). `_resolve_endpoint` then dropped its
synthetic third branch and now **raises** on an unresolvable endpoint (fixes S3-4). GSNR shifted
(the toy forward/backward moved from 18.85/17.53 dB to a symmetric ~17.81 dB from the added drop
penalty), but no test pins an absolute toy GSNR — the syn-vs-file gate holds within `TOL_DB` because
the file and the synthesized model symmetrized together. `test_terminal_roadm`'s no-penalty baseline
(a bare-transceiver terminus) became unphysical and was reframed to assert the penalty within one
propagation. **Decision rationale:** Option A (a "line-terminal transceiver" that keeps the toy
asymmetric) was empirically shown to keep GSNR pinned to the µdB, but was rejected on the principle
that *a transceiver hanging off the line with no ROADM is not a real optical terminal, even in a
test fixture* — the model should not encode nonsense topologies.

The historical entanglement analysis that motivated Option B is preserved below.

---

Attempting the migration during Batch C7 surfaced an entanglement the investigation note
underestimated. **The blocker is `_toy_model_synthesized`, the ground-truth gate itself.**
It mirrors `topologies/toy_2span.json`, which is an **asymmetric line-terminal topology**:
```
trx A → ROADM A → booster → fiber → ILA → fiber → preamp → trx Z
```
— a ROADM only at the **add** (A) end, a **bare transceiver at Z** with *no* drop ROADM,
and crucially **no reverse `ROADM A → trx A` edge** (the file wires `trx A → ROADM A`
one-directionally). Two consequences make the "self-contained test refactor" framing wrong:

1. **Registering the terminal transceivers triggers the bidirectional site loop.** The
   transceiver-site connection loop (`synthesize.py:128-130`) wires `trx ↔ roadm_<site>`
   in **both** directions. Registering `trx_A` at `site="A"` (roadm_A present) therefore
   adds a `roadm_A → trx_A` edge absent from `toy_2span.json`, changing the synthesized
   GNPy graph and risking a GSNR shift past `TOL_DB` — the exact gate the note named as the
   PR guardrail. The current synthetic branch avoids this: it wires `trx_A → roadm_A`
   (OMS-src direction) only.
2. **"Let OMS endpoints resolve to ROADMs" implies adding a drop ROADM at Z.** With C2
   Step B now propagating the terminal drop ROADM's `add_drop_osnr`, terminating the OMS at
   a `roadm_Z` adds a drop penalty the file-loaded reference (bare `trx Z`) does not have,
   so the synthesized-vs-file comparison diverges and `toy_2span.json` itself would need
   re-pinning. That is a **topology change, not a fixture rename.**

**Production impact: none** — `model_from_abstract_graph` always names `roadm_<id>` and
registers `trx_<id>`, so production never hits the synthetic branch; this is pure cleanup
plus the S3-4 "mistyped ROADM errors loudly" bonus, which stays open until it lands. The
clean landing needs a deliberate design decision (captured as a refined task in the Batch
C7 fix plan): either (a) introduce an explicit **line-terminal transceiver** concept
(registered transceiver whose `site` has no co-located ROADM) and **guard the site loop**
with `if roadm_<site> exists`, so an asymmetric bare-transceiver terminus is expressible
without the synthetic fallback; or (b) symmetrize the toy reference (ROADM at both ends),
re-pin ground truth, and adopt the importer convention wholesale.

---

## Stage 3 addendum — Topology importer (Phase 6a, `model/topology_import.py`)

**File:** `src/.../model/topology_import.py`
**Tests:** `tests/model/test_topology_import.py`

> **RESOLVED — Batch C7 (2026-07-09).** Addendum points 1 and 2 fixed. Point 1:
> `model_from_abstract_graph` registers a `FiberType` per distinct `fiber_type` named in
> the graph (defaults for non-SSMF physics, shared loss coef), so a non-SSMF edge no
> longer crashes `add_fiber`. Point 2: `_edge_spans` raises when `span_lengths_km` are
> present but do not sum to `length_km` (was silently re-derived), and
> `model_from_abstract_graph` raises when `amplifier_nf_db` length differs from the span
> count (was silently truncated/DEFAULT-filled by `nfs[i]`). Points 3–5 remain (Low, doc-only).

The importer is the model-build half of Phase 6a; the synthesis half (`synthesize.py`,
`adapter.py`) is Stages 3 & 4 above. Call order: `model_from_abstract_graph(graph, modes)`
(topology_import.py:59) → per node adds `roadm_/trx_/router_<id>`; per edge `_edge_spans`
(:52, → `split_link_into_spans` :7 when `span_lengths_km` is absent/inconsistent) then
`_add_directed_oms` (:89) **twice** (both directions), emitting a booster + per-span
`(fiber, amp)` + an `OMS` whose elements start at `roadm_<src>`.

1. **Heterogeneous fiber types crash the importer [Medium].** `model_from_abstract_graph`
   registers only `FiberType("SSMF", …)` (topology_import.py:67) but forwards each edge's
   `fiber_type` into `add_fiber`, which raises `ValueError` on an unknown type
   (network.py:50). Any abstract graph naming a non-SSMF `fiber_type` fails at import.
   Import-layer twin of Stage 3 point 1. **ACTION: register a `FiberType` per distinct
   `fiber_type` seen in the graph (or document SSMF-only).**
2. **Re-derived spans silently misalign per-span NF [Medium].** `_edge_spans` (:52) honours
   `span_lengths_km` only when `abs(sum − length_km) < 1.0` km; otherwise it discards them and
   re-derives via `split_link_into_spans`, which can yield a **different** span count.
   `amplifier_nf_db` is indexed by span position (`nfs[i]`, :104) with a `DEFAULT_AMP_NF_DB`
   fallback, so a re-derived count silently drops or defaults the intended per-span NFs.
   **ACTION: when span_lengths are rejected, also reconcile/renormalise the NF list, or fail
   loudly on a length/NF-count mismatch.**
3. **Booster gain is a hardcoded 20 dB, not derived from the launch delta [Low].**
   `_add_directed_oms` sets the booster to `DEFAULT_AMP_GAIN_DB = 20.0` (:96), contradicting
   phase-6a design decision 4 ("booster gain set so the booster lifts the ROADM's
   `target_pch_out_db` to the per-span launch power"). Under `power_mode=True` design
   (`gnpy_design_network`, synthesize.py:166) GNPy re-derives amp gains, so `Amplifier.gain_db`
   is largely decorative at propagation — the per-amp gain is neither honoured nor
   intended-correct. Cross-ref the `gnpy-power-mode-decision` memory.
4. **`Fiber.a_end`/`z_end` disagree with OMS element order and are unused by synthesis
   [Low].** `_add_directed_oms` sets `fiber_0.a_end = roadm_src` (:102), skipping the booster
   that sits between them in `OMS.elements`. Synthesis wires connections from OMS element
   order (synthesize.py:131), never from `Fiber.a_end/z_end`, so the fiber endpoints are
   decorative and misleading to anyone auditing physical adjacency from the `Fiber` fields.
5. **`edge["num_spans"]` is never read [Low].** `_edge_spans` uses `span_lengths_km` or
   derives from `length_km`; a graph whose `num_spans` disagrees with `len(span_lengths_km)`
   imports silently with no cross-check.

**Importer assumptions (record explicitly):**
- One ROADM/Transceiver/Router per node; `roadm_<id>` / `trx_<id>` / `router_<id>` naming.
  `Router.site == optical-node id` is the `src_router → optical node` convention the
  restoration design (Stage 7) depends on.
- Every edge is bidirectional → two independent directed OMS with independent amp chains
  (`amp_<src>_<dst>_*` vs `amp_<dst>_<src>_*`). The importer builds correct **reverse-chain**
  impairments — which Stage 4 finding A2 shows the adapter then discards (backward =
  `reversed(forward uids)`), so asymmetric NF on the reverse chain never reaches propagation.

### Phase 6a synthesis — re-confirmation (cross-references, no new numbered findings)

- **Stage 3 point 7 (temp-dir leak) confirmed still-present and amplified.**
  `model_to_gnpy_equipment` still `mkdtemp()`s per call (synthesize.py:43), `_equipment_from_dict`
  may `mkdtemp()` again (:210), and `_adv_config_path` now writes one `adv_nf_*.json` per
  distinct NF into it (:29). `build_gnpy_network` runs on every `compute_qot`.
- **Stage 3 point 11 (`_synthetic_trx` legacy branch) confirmed** at synthesize.py:100,
  121-123, 146-147 — unchanged from the investigation note above.
- **GNPy version drift [Medium — reproducibility].** `pyproject.toml:13` and
  `requirements.txt:2` pin **`gnpy==2.11.1`**, but `synthesize.py` uses the 2.14+ API
  (`design_network` :173, `load_equipment(file, extra_configs)` :214) and the project memory
  records the installed env as **GNPy 2.14.0**. A clean `pip install` per the declared pin
  would yield 2.11.1, where `design_network` does not exist → `build_gnpy_network` breaks.
  CLAUDE.md requires pinning the version for ground-truth stability; the declared pin, the
  code's API surface, and the actual env now disagree. **ACTION: bump the pin to the version
  the code targets (2.14.x) and re-pin the ground-truth tests against it.**

---

## Stage 4 — Physical layer propagation

**File:** `src/.../gnpy_adapter/adapter.py`
**Tests:** `tests/gnpy_adapter/test_compute_qot.py`, `tests/gnpy_adapter/test_per_direction.py`, `tests/gnpy_adapter/test_recompute_under_loading.py`, `tests/gnpy_adapter/test_toy_2route.py`

Full findings and the redesign rationale live in `stage-4-physical-layer-findings.md`.
The action plan below is the **revised** decomposition (post-review): it keeps the
confirmed findings A1–A5 and the per-OMS load decision (B), but **rejects the
C1 bundling** (which coupled the cheap direction fix to an expensive splice and
over-specified a per-carrier-identity prerequisite) and **defers the C2/C3
fast-path / NLI-overload optimization** as premature. See "Why not C2/C3" below.

### Confirmed findings (unchanged from the findings doc)

1. **Single-channel dummy injection** (adapter.py:45–65). A dummy at `probe_freq + 100 GHz` is added when loading has exactly 1 channel. NLI cross-talk verified < 0.1 µdB on the 2-span toy only. Band-edge latent bug: if the probe sits near the top of the SI band (191.3–196.1 THz), `probe + 100 GHz` lands out of band. *(= findings A6, low.)*
2. **Backward direction = reversed forward uid list** (adapter.py:178–179). `compute_qot` does `reversed(uids)` on the *forward* OMS's elements instead of resolving the physically separate reverse OMS. The importer builds two independent directed OMS per link (`oms_<src>_<dst>` ↔ `oms_<dst>_<src>`, `_add_directed_oms`, topology_import.py:82/109) with their own amps (`amp_<src>_<dst>_*` vs `amp_<dst>_<src>_*`). Backward QoT therefore never sees the reverse chain's impairments — the asymmetric-degradation feature CLAUDE.md promises is silently discarded. *(= findings A2, high.)*
3. **Backward ROADM `add_drop_osnr` penalty dropped** (adapter.py:242–243, 290–292). The reversed path's `(from_degree, to_degree)` pairing is not registered in the unidirectional GNPy graph, so the ROADM is skipped and its penalty is never added → backward GSNR is **optimistically biased**. Because `gated_qot` returns `min(fwd, bwd)`, an inflated backward value can win the min and wrongly flip `mode_feasible` to True — a confident-wrong "feasible" verdict feeding the margin gate. Same root cause as point 2. *(= findings A3, high. NB: the old roadmap wording "forward is always lower than backward" is **false** and contradicted by the project's own ground truth — fwd ~18.85 > bwd ~17.53 dB.)*
4. **Terminal drop-ROADM dropped in the *forward* direction too** (synthesize.py:137–143). Each OMS owns its source ROADM as `chain[0]`; the final drop ROADM of a path is nobody's `chain[0]`, so it is never propagated and its drop-side `add_drop_osnr` is omitted even forward. The toy hides this (destination is `trx Z`, not a ROADM). *(= findings A4, medium.)*
5. **Probe channel identified by first `mode_id` match, frequency ignored** (adapter.py:184; recompute passes `lp.mode_id` but never `lp.center_freq_hz`, adapter.py:365–368). In any WDM load with several same-mode channels, *every* same-mode lightpath is evaluated at the first matching channel's frequency; each lightpath's real `center_freq_hz` is ignored and all same-mode lightpaths return identical QoT. Directly undermines the load-bearing `recompute_qot_under_loading`. *(= findings A1, high.)*
6. **Global comb applied to every path + a concat bug** (whatif.py:28–39, adapter.py:365–371). `loading_from_model` builds **one** flat comb from *all* committed lightpaths and `recompute_qot_under_loading` propagates it through *every* path — channels on other fibers are counted as co-propagating interferers (NLI over-count). Worse, `loading_from_model` **concatenates** channels without `union`, so two lightpaths reusing a wavelength on disjoint OMS produce **two SI carriers at the same frequency** → degenerate `slot_width = freq[1]-freq[0]` and malformed NLI. This is a bug, not just imprecision. *(= findings B1; the concat case is sharper than the doc states.)*
7. **Limiting-element diagnostic is meaningless** (adapter.py:304–315). The minimum GSNR delta is taken *including* `-inf`, so the first noise-introducing element (booster) always produces `finite − inf = -inf` and is always chosen. `limiting_element_id` is almost never the physically most-limiting span. *(= findings A5, low — KEEP; see step F.)*
8. **`_find_launch_transceiver` looks for a direct predecessor** of `path_uids[0]` (adapter.py:106–119). A `None` result means `si` enters the chain without tx_power init. Verify for both toy and synthesized topologies. *(diagnostic; verify only.)*

### Revised action plan (ordered; each step is independently shippable)

The steps are sequenced by **value ÷ risk**, not by finding number. A/B/C are the
correctness core; D is the real perf fix; E/F are cleanup. Each carries its own
test and must re-pin `test_ground_truth_bridge.py` GSNR within `TOL_DB`.

- **Step A — Resolve the paired reverse OMS (fixes findings 2 + 3).** For
  `Direction.BACKWARD`, walk the actual reverse OMS chain in its natural order
  instead of `reversed(uids)`. The importer's naming is deterministic
  (`oms_<src>_<dst>` ↔ `oms_<dst>_<src>`, or match on swapped `{src,dst}` nodes),
  so this is a lookup, **not** a comb splice and **not** a new data model. Backward
  then walks `amp_<dst>_<src>_*` and hits ROADMs on registered forward pairings, so
  the `add_drop_osnr` penalty is applied. *Test (findings D2/D3): a multi-OMS,
  multi-ROADM path where asymmetric NF on the reverse chain moves backward QoT and
  forward is unchanged.* **Highest value, lowest risk — do first; ship alone.**

- **Step B — Walk through the terminal ROADM (fixes finding 4).** Include the
  path's final drop ROADM in the propagated element list (append `roadm_<dst>` to
  the terminal OMS's elements, or handle the drop ROADM explicitly after the loop)
  so its drop-side `add_drop_osnr` is applied forward and backward. *Test
  (findings D3): an importer-style path terminating at a ROADM asserts the terminal
  drop penalty is present.*

- **Step C — Probe by frequency + per-path interferers (fixes findings 5 + 6).**
  Two coupled changes:
  - **C1 (probe by frequency):** thread `center_freq_hz` into `compute_qot` /
    `recompute_qot_under_loading`; select the probe by frequency (fall back to
    `mode_id` only when unambiguous). *Test (findings D1): multiple same-mode
    channels at different frequencies; assert each lightpath gets its own
    frequency's QoT.*
  - **C2 (per-path interferer comb from the existing bitmask):** replace
    `loading_from_model`'s global concat with the per-path construction
    `allocation._build_loading` **already uses** — build each lightpath's interferer
    set from `occupied_along(state, lp.oms_sequence)`, i.e. only the channels lit on
    *its own* OMS. This kills the over-count **and** the duplicate-frequency concat
    bug, reusing tested machinery. **No new `lightpath_id` field on `Channel` is
    required:** QoT is probe-at-a-time (`compute_qot` reads one `probe_idx`), and
    NLI depends on interferer *power*, not interferer noise — so carrying the probe
    and rebuilding interferers from the per-OMS bitmask is sufficient. The
    per-carrier-identity prerequisite in the findings doc's C1 is induced only by
    the deferred C2/C3 read-all-carriers optimization; without it, it evaporates.
    *Test: a lightpath whose only interferers are on a disjoint fiber gets clean
    single-carrier QoT (no phantom co-propagators).*

  > **Express-path note (deferred sub-item, not blocking):** a per-path single comb
  > is exact for single-OMS paths and slightly pessimistic for **express (multi-OMS)
  > paths** where interferer membership differs per OMS. Before building the full
  > per-OMS splice (findings C1), **quantify the over-count** on a representative
  > express path; if it is under `TOL_DB`, the single per-path comb is enough and the
  > splice is unnecessary. Only build the splice if the measured error justifies it.

- **Step D — Cache the synthesized GNPy network/equipment (the real perf fix).**
  `build_gnpy_network` runs on **every** `compute_qot` — 2N times per bulk recompute
  — and `model_to_gnpy_equipment` leaks a `tempfile.mkdtemp()` each call (Stage 3
  point 7 / findings A7). Cache synthesized equipment+network per model-fingerprint
  (invalidate on mutation) or scope a context-managed temp dir to the adapter
  lifecycle. **This — not C2/C3's O(N)→O(N²) NLI micro-optimization — is the
  dominant per-query cost.** *Test: a bulk recompute over K lightpaths performs one
  synthesis, not K; no orphaned temp dirs remain.*

- **Step E — Band-edge dummy placement (finding 1 / A6).** When the probe is near
  the upper SI band edge, place the dummy *below* the probe so it stays in band.
  Largely moot once Step C populates a real interferer backdrop, but keep the guard
  for genuinely single-channel OMS. *Test (findings D5): single-channel probe on a
  band-edge path; dummy stays in band, cross-talk bounded.*

- **Step F — Fix the limiting-element diagnostic (finding 7 / A5 — KEEP).** Exclude
  the `inf→finite` first-noise transition; choose the limiting element from
  finite-to-finite GSNR deltas only (or by absolute GSNR drop in the noise-loaded
  region). Before refining: confirm a consumer needs `limiting_element_id` at all —
  no CLAUDE.md tool currently reads it, so "make the diagnostic correct" and "drop
  the diagnostic" are both on the table; pick one deliberately rather than leaving
  it silently wrong. *Test (findings D4): the limiting element is a noise-loaded
  element, not the first amplifier by construction.*

- **Step G — Verify `_find_launch_transceiver` (finding 8).** Confirm a Transceiver
  predecessor of `path_uids[0]` exists for both toy and synthesized topologies; a
  `None` result silently drops tx_power init. Verification only unless it fails.

### Why not C2/C3 (deferred, with rationale)

The findings doc's C2 (two-engine single-channel fast path) and C3 (overload
GNPy's per-channel NLI extraction) are **deferred, not adopted**:

- **Wrong bottleneck.** The doc concedes the O(N) fast path does *not* help bulk
  recompute (N single passes ≈ one full-comb pass, both O(N²)). For N≈48, N²≈2300 —
  trivial arithmetic. The dominant per-query cost is the GNPy network rebuild +
  design pass (Step D), orders of magnitude larger than the NLI sum.
- **Highest-risk surface.** C3 overloads the NLI/Raman solver — the exact GNPy
  internal the doc itself flags as numerically version-sensitive — to win an
  optimization that probably isn't needed.
- **Induces the identity prerequisite.** C2's "read all carriers from one full-comb
  pass" is the *only* reason the doc claims per-carrier `lightpath_id` is required.
  Dropping C2/C3 removes that data-model change (see Step C).

Revisit C2/C3 only if profiling *after Step D* shows the NLI computation itself is
the bottleneck — and gate any overload against full-comb GNPy `nli[probe_idx]`
within `TOL_DB`, pinned per GNPy release.

### Load-model decision (B, adopted)

Model the **actual per-OMS load**; offer full-grid worst-case fill as one
*constructible* `LoadingState` (a thin helper), not a separate engine. Rejected the
full-load-always shortcut: it would no-op the make-before-break survivor-QoT change,
freeze the margin-feasibility gate, and collapse `whatif_margin_threshold_sweep`'s
"right now" — all explicit CLAUDE.md commitments. Granularity is **per-OMS** (channels
add/drop/express only at ROADMs = OMS boundaries), aligning the QoT interferer comb
with the RSA spectrum bitmask that already exists (`build_spectrum_state`,
`occupied_along`). Step C is the first consumer of this alignment.

---

## Stage 5 — IP layer and coupling

**Files:** `src/.../model/ip_routing.py`, `src/.../model/network.py`
**Tests:** `tests/model/test_ip_routing.py`, `tests/model/test_layer_consistency.py`, `tests/model/test_capacity_coupling.py`

1. **`simulate_ip_routing` does not reroute.** Load is applied to the pinned `working_path` only. Services are only "dropped" when a link is **down** (capacity = 0). A link at 200% utilization keeps all services "running". Confirm `dropped_services` is not used as a proxy for "impacted services" anywhere — congestion and down are distinct states.
2. **`build_grooming_map` can produce duplicate LP IDs in `by_service`.** If a service's path crosses the same lightpath twice (routing loop), `by_service[svc.id]` contains duplicates (ip_routing.py:42, no dedup). The reverse map deduplicates but the forward map does not. **ACTION: decide whether `by_service` should preserve path order with duplicates (current behavior) or dedup, and document the choice.**
3. **`is_contiguous_path`** allows each IP link to be traversed in either orientation — correct for undirected IP topology over directed lightpaths.

### Deeper findings (Phase 5 inspection, 2026-07-07)

Call order for the headline read: tool → `simulate_ip_routing(model)` → `offered_load_per_link`
(sum each service's `demand_gbps` onto its pinned `working_path`) → per link
`ip_link_capacity_gbps` (lightpath → `QoTState` → margin gate → mode bitrate) → classify
down/congested/overflow → second pass over services to attribute drops to down links.
`ip_link_capacity_gbps` (network.py:223–231) is the single derived-capacity chokepoint; its
two edge behaviors — returns `0.0` on `margin_db < 0` (line 230), **raises `LookupError`** on
absent QoT (line 228) — propagate into every caller, and callers handle them inconsistently.

4. **`simulate_ip_routing` is not total — it can raise `LookupError` [High].** `simulate_ip_routing` (ip_routing.py:102) calls `ip_link_capacity_gbps` with no guard; that method raises `LookupError` (network.py:228) when the bound lightpath has no recorded QoT. A lightpath+IP link provisioned before `recompute_qot_under_loading` makes this read tool throw straight out of the MCP surface. `views.ip_topology_dict` already guards this exact case (→ `capacity_gbps: None`); `simulate` does not. This violates the "read tools return structured data, never raise" contract. **ACTION: wrap the capacity read in `simulate_ip_routing` in the same try/except as `ip_topology_dict`; treat "no QoT recorded" as a distinct state (capacity `None` / `unknown`) rather than crashing.**
5. **`down_links` excludes idle-but-down links [Medium].** The guard at ip_routing.py:106–108 appends to `down_links` only when `offered > 0`, yet `LinkUtilization.down` is `True` regardless of load. A consumer reading `down_links` to enumerate outages silently misses down links carrying no traffic; the full truth is only in `utilizations[].down`. **ACTION: either rename/re-document `down_links` as "down-and-loaded", or populate it with every down link and report the loaded subset separately — make the field name match the contract.**
6. **Per-direction optical asymmetry never reaches the IP layer [Medium].** `ip_link_capacity_gbps` reads a single `QoTState` per lightpath, so the IP capacity gate collapses the per-direction optical QoT to one scalar. An asymmetric degradation (one fiber direction down — the scenario CLAUDE.md promises the disaster consumer will generate) cannot manifest as a directional IP capacity change; the IP layer is undirected by construction. **ACTION: document this as a known modeling boundary; if directional IP capacity is ever needed, it requires a per-direction `QoTState` keyed by `(lightpath, direction)`.**
7. **The failure→drop coupling lives in `inject_failure`, not in `simulate` [assumption].** `simulate_ip_routing` never consults `is_failed`/`_failed_assets`; it trusts that `whatif.inject_failure` (whatif.py:91–96) has already written a `margin=-inf` sentinel on every crossing lightpath. Calling `model.mark_failed(...)` directly (without going through `inject_failure`) will **not** surface as a drop — `_failed_assets` is decorative for the simulate path. **ACTION: document that `mark_failed` alone is insufficient (the QoT sentinel is the load-bearing mechanism), or have `simulate` additionally treat any lightpath crossing a `_failed_assets` element as down.**
8. **`_residual_gbps` recomputes the whole offered-load map per lightpath [Low].** `multilayer_graph._residual_gbps` (multilayer_graph.py:80) calls `offered_load_per_link(model)` inside the per-lightpath loop of `build_layered_graph` (line 119) → O(L·(S+E)) rebuilds of the same map. Rooted in Phase 5's `offered_load_per_link`. Its `max` over bound IP links (line 84) can also overstate grooming residual when one of several links on a lightpath is saturated (cross-ref Stage 7 point 2). **ACTION: hoist the `offered_load_per_link` call — build the map once and pass it in.**
9. **`overflow_gbps` and dropped demand are non-disjoint [Low].** A service crossing both a congested link and a down link contributes to `overflow_gbps` (ip_routing.py:111) *and* appears in `dropped_services`. The metrics are distinct by design, but a consumer summing them double-counts that demand. **ACTION: document that `overflow_gbps` and dropped demand must not be summed as "total lost traffic".**

**Stage 5 assumptions (record explicitly):**
- Every IP link's lightpath has a recorded QoT state (violated pre-`recompute_qot_under_loading` — see finding 4).
- `working_path` is the only load-bearing path; `protection_path` is standby, contributing zero load (decision 3).
- The IP layer is undirected: a single capacity scalar per link, either-orientation traversal in `is_contiguous_path` (see finding 6).
- `offered_load_per_link`'s dict is keyed only by currently-existing IP links; pinned paths always reference live links (guaranteed by `add_service` / `set_service_working_path` validation; no `remove_ip_link` exists).

---

## Stage 6 — Routing solvers

**File:** `src/.../model/solvers.py`
**Tests:** `tests/model/test_solvers.py`, `tests/model/test_avoidance.py`

1. **OMS graph is undirected (`nx.MultiGraph`).** If the topology has unidirectional OMS (e.g., ring fibers), the solver offers routes that don't physically exist. Confirm `topology_import.py` creates OMS in both directions for bidirectional spans.
2. **`_DISJOINT_CANDIDATE_CAP = 32`.** On a dense topology a fully-disjoint pair beyond the 32nd candidate causes a silent `NO_SOLUTION`. Confirm `test_solvers.py` covers a case where the disjoint pair is not among the top-k paths.
3. **`forbidden_oms` adds endpoint nodes to `phys`.** Placing a ROADM id in `avoid_assets` blocks all OMS through that ROADM — correct for node avoidance but surprising if you intended only to block one fiber at that site.

### Deeper findings (Stage 6 inspection)

Call order: `compute_paths` (solvers.py:172) → `_avoid_sets` (:87) → `forbidden_oms` (:94) →
`_enumerate_oms_paths` (:130) → `build_oms_graph` (:62) + collapse to a `simple` graph (:145) +
`nx.shortest_simple_paths` (:158) + per-hop `_oms_between` (:117) + `itertools.product` (:165).
`compute_disjoint_paths` (:228) shares the avoid/forbidden resolution, enumerates up to
`_DISJOINT_CANDIDATE_CAP=32` (:59) candidates, then runs an O(n²) pairwise `path_basis_keys`
(exposure.py:74) scan (:247). `check_disjointness` (:202) is the audit primitive.

4. **Bidirectional link → two undirected parallels → reverse-direction OMS offered as a
   route, and as a fake "disjoint" pair [High].** The importer's `_add_directed_oms` runs twice,
   emitting `oms_<src>_<dst>` and `oms_<dst>_<src>` for one span (topology_import.py:83,109).
   `_oms_between` matches on the unordered set `{src,dst}` (solvers.py:124), so both directed OMS
   are returned as candidate routes for one direction. Consequences: (a) `compute_paths(A,B)`
   emits the reverse-direction OMS, so a lightpath built from that sequence carries a B→A OMS
   inside an A→B route (feeds Stage 4's naming-based reverse resolution wrong); (b)
   `compute_disjoint_paths(A,B, basis="physical")` can return `(oms_A_B,)` + `(oms_B_A,)` as a
   "disjoint pair" — the two directions of the same physical span as working+protection, passing
   the physical audit while sharing duct/structure risk. Tests miss it (fixtures add one
   undirected OMS per node-pair; only importer-built models have both directions). Concrete
   opposite-facing case of point 1. **ACTION: decide whether the OMS graph should be directed, or
   filter `_oms_between`/disjoint enumeration to the OMS whose `(src,dst)` matches the requested
   travel direction; add an importer-style (both-directions) test to solvers.**
5. **`weight="length"` k-shortest is only approximately length-ordered [Medium].** The `simple`
   graph gives each edge the *minimum* parallel-OMS length (solvers.py:149), so node paths are
   ranked by best-case parallel; hop expansion then emits *all* parallels per hop in odometer
   order (`itertools.product`, :165) while counting toward `k`. A long parallel on an earlier node
   path is emitted before a shorter route on a later one, and multi-hop combos aren't total-length
   sorted. The first-`k` are not the true k-shortest by fiber km, and the cap-32 window can miss
   the shortest disjoint pair. Honest heuristic. **ACTION: document the non-guarantee (RSA relies
   on `weight="length"`).**
6. **Parallel expansion inflates the candidate list, amplifying the cap-32 blind spot [Medium]
   (sharpens point 2).** `_DISJOINT_CANDIDATE_CAP` counts `(node-path × parallel-combo)` emissions,
   not distinct node paths (solvers.py:165-169). High parallelism exhausts the 32 candidates on
   variations of the first node path(s), so a topologically distinct disjoint route never enters
   the pairwise scan → false `NO_SOLUTION`. **ACTION: cap by distinct node paths (or raise/
   parameterize the cap) so parallels don't starve topological diversity.**

> **Design note — why not a guaranteed disjoint-path algorithm (Yen's / Suurballe)?**
> - **Yen's is not a disjoint-path algorithm, and it is already in use.** `nx.shortest_simple_paths`
>   (solvers.py:158) *is* Yen's k-shortest-loopless-paths. `compute_disjoint_paths` is already
>   "Yen's + an O(n²) pairwise basis-key filter over a cap-32 candidate list" — the cap is the
>   weakness (finding 6), not the enumerator. Switching "to Yen's" changes nothing.
> - **The guaranteed algorithm is Suurballe/Bhandari**, not Yen's. For `basis=physical,
>   level=link|node` it finds a min-cost disjoint *pair* in polynomial time with no cap and no
>   false `NO_SOLUTION` — a real available fix for finding 6, but only in the physical case.
> - **It does not generalize.** (a) SRLG / risk-group / union disjointness is **NP-hard** (Hu
>   2003): two paths can be link-disjoint yet share an SRLG (scenario 1 — SRLG-disjoint but both
>   aerial in one storm cone), so no polynomial guarantee exists and the enumerate-and-check
>   heuristic must stay for `basis ∈ {srlg, risk_group, union}`. (b) Suurballe is all-or-nothing
>   and cannot produce the `best_effort` minimum-overlap fallback CLAUDE.md requires. (c) It is
>   orthogonal to finding 4 — on the undirected graph it would still return the two directions of
>   one span as "disjoint".
> - **Honest shape:** a hybrid — Suurballe for physical link/node disjointness; enumerate+filter
>   for the risk-based bases and for best-effort. Aligns with CLAUDE.md's no-optimality-claim
>   contract. A Suurballe fast-path is a *future* code change (its own PR), not part of this
>   inspection.

7. **The avoid key `risk_groups` also matches SRLG ids (misnomer + collision) [Low].**
   `forbidden_oms` iterates `list_srlgs() + list_risk_groups()` and matches `g.id in
   avoid_risk_groups` (solvers.py:104-105); `test_avoid_parallel_in_different_srlg_keeps_the_other`
   confirms an SRLG id resolves through the `risk_groups` key. The name misleads (it means "named
   groups: SRLG *or* risk-group") and an SRLG/RiskGroup id collision expands both. **ACTION:
   document the intended semantics (or split the key).**
8. **Best-effort disjoint pair minimizes shared-key *count*, not physical severity [Low].**
   `len(shared) < best_overlap` (solvers.py:256) prefers the fewest namespaced keys. Under `union`,
   one large shared SRLG (1 key) ranks "better" than two shared amps (2 keys) regardless of
   correlated-asset volume. "Minimum-overlap" = fewest keys, not least risk. **ACTION: document,
   or weight overlap by asset count / severity.**
9. **Latent coupling: `oms_length_km` + the two forbidden-filters must stay in sync [Low].**
   `oms_length_km` silently treats any non-fiber/unresolvable element as 0 km (solvers.py:80-83).
   The length-weight line `min(oms_length_km(o) for o in _oms_between(u,v,forbidden))` (:149)
   assumes `_oms_between` is non-empty for every pruned-graph edge — true only because
   `build_oms_graph` and `_oms_between` filter on the same `forbidden` set. Diverging filters →
   `ValueError` on empty `min()`. Currently correct; **ACTION: note the invariant.**

**Stage 6 assumptions (record explicitly):**
- OMS routing graph is undirected (`nx.MultiGraph`), one edge per OMS, parallels preserved —
  see point 4 for the bidirectional-span consequence.
- Avoidance is layer-agnostic per-OMS-edge pruning, applied twice (`build_oms_graph` and
  re-threaded through `_oms_between`) — the design's key correctness property; it holds.
- `avoid.assets` intersects the OMS asset set (oms id ∪ fiber/amp/roadm uids) plus endpoint nodes,
  so a node id in `avoid.assets` prunes every OMS through it (point 3).
- Enumeration is deterministic: `_oms_between` sorts by `(length, id)` or `id`.
- Disjointness keys are namespaced (`phys:`/`node:`/`srlg:`/`rg:`) so `union` never collides.

---

## Stage 7 — Multilayer graph and restoration

**Files:** `src/.../model/multilayer_graph.py`, `src/.../model/restoration.py`
**Tests:** `tests/model/test_multilayer_graph.py`, `tests/model/test_restoration.py`, `tests/model/test_allocation.py`

1. **`_lightpath_endpoints` uses `first_oms.src_node_id` / `last_oms.dst_node_id`.** Incorrectly chained OMS sequences (Stage 1 point 4) produce wrong LPE endpoints and silently misroute grooming candidates.
2. **`_residual_gbps` uses `max` over multiple IP links.** For a lightpath serving two IP links, residual = max(cap − load) across links. This surfaces the higher residual of the two — correct for grooming opportunity but can overstate available capacity if one link is saturated.
3. **TxE/RxE added in both directions** for every free wavelength slot. The k-shortest search sees candidates in both directions simultaneously; confirm the deduplication in `place_demands` prevents symmetric duplicates from filling the k-best frontier.
4. **`place_demands` deduplicates on route, not wavelength.** Same OMS sequence on different wavelength slots yields one placement entry. The returned candidate does not commit to a specific wavelength — provisioning must re-run spectrum assignment.

> **Current consumer scope:** `build_layered_graph` / `place_demands` are imported only by
> `restoration.py` — the layered graph routes **restoration** today, not new services.
> New-service routing (`solve_rsa`, `solve_allocation`, allocation.py) still uses the flat
> OMS `compute_paths` solver (Stage 6). The design commits `solve_allocation` to this graph as
> the **next** consumer (deferred), so findings 5 and 9 below graduate from restoration edge
> cases to general-routing correctness issues once that refactor lands.

### Deeper findings (Stage 7 inspection)

Call order (the `compute_restoration` read): `compute_restoration` (restoration.py:78) →
resolve src/dst via `model.get_router(svc.src_router).site` (:84-85) → `_forbidden_assets`
(:38, avoid.assets + SRLG/risk-group members, NOT endpoint nodes) → `build_layered_graph`
(multilayer_graph.py:88): access vertices (:111), LPE per lightpath via `_lightpath_forbidden`
(:56)/`_residual_gbps` (:68 → `ip_link_capacity_gbps` margin gate + `offered_load_per_link`),
WLE/TxE/RxE per free slot from `build_spectrum_state` (:127-141) → for policy in
(`groom_or_new`,`new_only`): `place_demands` (:232) → `_policy_graph` drop TxE/LPE (:184) →
`nx.shortest_simple_paths` on weight (:251) → `_parse_path` (:200) → intra-bucket dedup by
(reused, oms_seqs) ignoring lam (:260) → per new run `_build_loading` (allocation.py:98) +
`_best_feasible_mode` (worse-of-two-directions GSNR → highest feasible mode, :115) →
`restored = min(demand, groom bottleneck, new mode rate)` (:278) → cross-bucket dedup by
(reused,(oms_seq,lam)) INCLUDING lam (:97-98) → `_lever` (:53) → sort (shortfall,
transponders, hops) (:104) → status.

5. **New-lightpath runs lose travel direction; `oms_sequence` may be a forward OMS traversed
   in reverse [Medium].** WLE edges are added in both `(u,v)` and `(v,u)`
   (multilayer_graph.py:136) but carry only `oms_id`; `_parse_path` records `oms_id` with no
   orientation (:213). A B→A demand over a physically A→B OMS yields
   `NewLightpathRun(oms_sequence=("oms-AB",))` (confirmed by
   `test_new_only_lights_new_lightpath_when_no_existing_path`). `_lightpath_endpoints` (:49) on
   that sequence reports endpoints A,B — the reverse of the intended B→A run. When Phase 7
   provisions it, endpoint derivation and Stage 4's naming-based reverse-OMS resolution (A2)
   both assume `oms_sequence` is in travel order → a mis-oriented lightpath. Cross-ref Stage 6
   finding 4, Stage 4 A2. **ACTION: record travel direction (or the reverse OMS id) in
   `NewLightpathRun`, or document that `oms_sequence` is physical-OMS order and provisioning
   must re-derive direction from src/dst.**
6. **`_PATH_BUDGET` (64) is consumed by wavelength-duplicate paths that dedup then collapses
   [Medium — sharpens points 3/4].** A single physical new-lightpath route appears once per
   free λ (up to 48), all identical weight, emitted consecutively by `shortest_simple_paths`.
   The loop advances `i` per emitted path (:251-252) but dedups by route-ignoring-λ (:260), so
   ~48 λ-variants of one route burn 48 of the 64-path budget for one placement. On a
   lightly-loaded survivor graph the budget can be exhausted on λ-variants of the cheapest
   route(s) before a structurally distinct route is reached (`new_only` bites hardest) →
   thinner frontier / missed survivor routes. **ACTION: advance the budget only on distinct
   routes (dedup before counting), or cap λ-variants per route.**
7. **Cross-bucket dedup includes λ while intra-bucket ignores it — same route can survive as a
   duplicate candidate [Low].** `place_demands` dedups on `(reused, oms_seqs)`
   (multilayer_graph.py:260); `compute_restoration` dedups on `(reused, (oms_seq, lam))`
   (restoration.py:97-98). If the representative λ differs between the `groom_or_new` and
   `new_only` passes (different graphs → different enumeration order), the same physical route
   escapes as two candidates. **ACTION: drop `r.lam` from the restoration key — a candidate
   doesn't commit to a wavelength anyway (point 4).**
8. **`_residual_gbps` rebuilds the offered-load map per lightpath; `max`-over-links overstates
   grooming residual [Low].** `offered_load_per_link(model)` (O(services)) is called inside the
   per-lightpath loop (multilayer_graph.py:80 from :119) → O(L·S) rebuilds; the `max` over a
   lightpath's bound IP links (:84) surfaces the highest residual, so a two-link lightpath with
   one saturated link reports the healthy link's residual. Same root as Stage 5 finding 8 and
   point 2 above. **ACTION: hoist the load map out of the loop; reconsider `max` vs `min`.**
9. **QoT probed only at `ref_mode` (first mode); GSNR mode-independence assumed
   [assumption/Medium].** `place_demands` builds loading with `ref_mode = model.modes.list()[0].id`
   (multilayer_graph.py:248,268) and `_best_feasible_mode` probes once then picks the highest
   feasible bitrate. Correct only while all modes share symbol rate/launch — documented in
   `allocation.py` but inherited silently. Breaks with multi-baud formats. Cross-ref Stage 2
   point 4. **ACTION: document the inheritance; re-probe at the delivered mode once per-format
   baud lands.**
10. **`_build_loading` for each new run ignores channels that other new runs on the same path
    would light [Low].** Each run is QoT'd against the committed spectrum snapshot only
    (multilayer_graph.py:247,268). Harmless today because successive runs are separated by
    grooming hops on different OMS (no shared-fiber NLI), but it is an unstated precondition.
    **ACTION: document that runs within one placement are assumed OMS-disjoint.**
11. **`cost_facets` are proxies and the final ranking sorts on them [Low].** `hops` =
    reused+new lightpath count (not fiber hops); `transponders` = 2×new runs (restoration.py:63-66).
    Candidates are ordered by `(shortfall, transponders, hops)` (:104) until `evaluate_objective`
    lands (design defers this). **ACTION: document the proxy semantics.**
12. **WLE availability and the loading comb are built from two independent
    `build_spectrum_state` calls [Low, latent coupling].** `build_layered_graph` (:99) and
    `place_demands` (:247) each rebuild spectrum from the model; they agree today, but passing a
    custom `grid` to one and the default to the other would desync the WLE layers from the NLI
    comb. **ACTION: thread one grid/spectrum through both, or note the invariant.**
13. **Parallel OMS collapse per wavelength on the `nx.DiGraph` WL layers [Medium — not
    previously recorded; found 2026-07-09 during the C6 S7-6 fix].** `build_layered_graph`
    adds WLE edges on a plain `nx.DiGraph` (multilayer_graph.py:98) via
    `g.add_edge((WL, a, lam), (WL, b, lam), oms_id=…)` (:137). When two **parallel OMS** share
    the same ordered node pair `a→b` and the same free slot `lam`, the second `add_edge`
    **overwrites** the first — only the last-added parallel OMS's wavelength layer exists per λ,
    so placement/restoration over parallel fibers silently considers only one of them per
    wavelength. Confirmed empirically: a fixture with two parallel A→B OMS yields only one cheap
    route in `place_demands` enumeration (the C6 S7-6 test was reworked to use a wide grid on a
    single route instead). Distinct from the flat-solver directed-graph issue (Stage 6 finding 4,
    fixed in C6); this is the *layered* graph. Harmless while restoration only routes over the
    toy/demo topologies, but becomes a correctness issue once `solve_allocation` graduates onto
    the layered graph (the deferred Stage 7 consumer) or a real multi-fiber topology is loaded.
    **ACTION: use a `MultiDiGraph` for the WL layers (mirrors the C6 S6-4 solvers change) so
    parallel OMS stay distinct per wavelength; add a two-parallel-OMS placement test.**

> **Re point 3 (TxE/RxE both directions):** the λ-ignoring intra-bucket dedup (:260) collapses
> "same route, different λ", so pure symmetric fills are handled; the residual risk is the
> cross-bucket λ mismatch in finding 7.

**Stage 7 assumptions (record explicitly):**
- `Router.site == optical-node id` is the src→optical resolution (design's flagged convention).
- Wavelength continuity is **structural** (no cross-λ edge); a new run is one λ end-to-end.
- Failed-lightpath consistency is triple-guarded — OMS pruned (`_oms_forbidden`), LPE dropped
  (`_lightpath_forbidden`), margin<0 zeroes residual — but only when the actual failed asset is
  in `avoid`.
- A candidate does **not** commit to a wavelength; provisioning re-runs spectrum assignment
  (point 4).
- Runs within one placement are assumed OMS-disjoint (finding 10).

---

## Stage 8 — What-if + injection (Phase 6b)

**Files:** `src/.../model/whatif.py`, `src/.../model/network.py` (injection mutators,
network.py:235-260), `src/.../model/snapshots.py` (clone/diff carry).
**Tests:** `tests/model/test_whatif.py`, `tests/model/test_injection_layer_consistency.py`,
`tests/test_server_phase6.py`.

Call order for the headline read: `inject_degradation(model, store, asset_id, nf/loss delta)`
(whatif.py:137) → snapshot `before` margins (:154) → `apply_nf_delta`/`apply_loss_delta`
(network.py:235/240) → `recompute_qot_under_loading(model, store, loading_from_model(model))`
(adapter.py:356, whatif.py:163) → build `DegradationRow`s (`crossed = feasible_before and not
feasible_after`, :172). `inject_failure` (whatif.py:78) is physics-free: `mark_failed` then a
`margin=-inf` sentinel on every lightpath crossing a failed asset. `margin_threshold_sweep`
(whatif.py:42) is a pure read of recorded margin. Margin is an **output** throughout.

1. **Failure and degradation do not compose — recompute resurrects a downed lightpath
   [High].** `inject_failure` (whatif.py:78-101) writes an `-inf` QoT sentinel per crossing
   lightpath, but `recompute_qot_under_loading` (adapter.py:356-371) calls `set_qot_state`
   for **every** lightpath unconditionally, and neither recompute nor synthesis consults
   `_failed_assets` (the failed fiber still propagates in the synthesized network). So any
   later recompute — including the one inside `inject_degradation` (whatif.py:163) —
   overwrites the sentinel with a **feasible** GSNR, silently restoring the "down" lightpath's
   capacity. *Failure scenario:* branch → `inject_failure(("fiber_0_1_0",))` downs `lp0`
   (cap 0, service dropped) → `inject_degradation` on any amp → recompute overwrites `lp0`'s
   sentinel → `lp0` feasible again → `simulate_ip_routing` no longer drops the service.
   **ACTION: have recompute/synthesis honour `_failed_assets` (skip failed lightpaths or
   force the sentinel), or re-apply `inject_failure` sentinels after any recompute.**
2. **Spurious "crossed" for lightpaths with no prior QoT [Medium].** In `inject_degradation`,
   `before` is built only from lightpaths that already have QoT (`_has_qot`, whatif.py:154-156);
   for any other lightpath `mb = before.get(lp.id, float("inf"))` → `feasible_before = mb >= 0`
   = **True** (:169-170). If recompute then finds it infeasible, `crossed = True` despite no
   feasible baseline — a false feasible→infeasible event. **ACTION: exclude lightpaths absent
   from `before` from the crossing set, or mark `feasible_before` unknown.**
3. **`inject_failure` cannot down a lightpath via its destination ROADM [Medium].** Crossing
   is tested with `oms_seq_asset_set(model, lp.oms_sequence) & failed` (whatif.py:89), but the
   importer's `OMS.elements` start at `roadm_src` and end at the last amp — the **dst ROADM is
   not an element** (topology_import.py:98,109), so it is absent from `oms_seq_asset_set`.
   `inject_failure(("roadm_<dst>",))` will not down a lightpath terminating there.
   Failure-layer mirror of Stage 4 finding A4.
4. **`feasible_before` and `feasible_after` use different definitions [Low].**
   `feasible_before = margin_before >= 0` (recomputed from the stored margin, :170) vs
   `feasible_after = st.mode_feasible` (gsnr ≥ required, :171). They coincide today
   (margin = gsnr − required) but the asymmetry means any future divergence silently splits
   the crossing logic. **ACTION: derive both from the same predicate.**
5. **`loading_from_model` bypasses `union`'s spectrum-clash check [Low].** It constructs
   `LoadingState(channels=tuple(channels))` directly (whatif.py:39); overlap is only detected
   in `LoadingState.union` (loading.py:30). Two lightpaths sharing a wavelength on disjoint
   OMS yield two same-frequency carriers with no error — the 6b entry point of the concat
   issue already flagged as Stage 4 finding 6.
6. **`clear_failed` desyncs from QoT [Low].** `network.clear_failed` (network.py:249) only
   empties `_failed_assets`; it never recomputes or restores the sentinelled lightpaths, so
   after clearing, the failed set and the QoT state can disagree until an explicit recompute.

**Stage 8 assumptions (record explicitly):**
- Injection mutates the model's physical parameters in place on a **branch**; correctness
  depends on `snapshots._clone` copying the mutated `_amplifiers`/`_fibers` **and**
  `_failed_assets` (snapshots.py:96) and `diff` reporting `failed_assets` (:76, `_delta_set`).
  Verified present.
- `inject_failure`'s `-inf` sentinel — not `_failed_assets` — is the load-bearing mechanism
  that reaches `simulate_ip_routing` (same point as Stage 5 finding 7).
- `margin_threshold_sweep` is a pure read; lightpaths with no recorded QoT are omitted
  (whatif.py:49-52); margin is never an input anywhere in the module.

---

## Test coverage map

| Stage | Source files | Test files |
|-------|-------------|-----------|
| 1 | `model/assets.py`, `model/network.py` | `test_assets.py`, `test_network.py`, `test_snapshot_lifecycle.py` |
| 2 | `gnpy_adapter/loading.py` | `test_loading.py` |
| 3 | `gnpy_adapter/synthesize.py` | `test_synthesize.py`, `test_ground_truth_bridge.py` |
| 4 | `gnpy_adapter/adapter.py` | `test_compute_qot.py`, `test_per_direction.py`, `test_recompute_under_loading.py` |
| 5 | `model/ip_routing.py` | `test_ip_routing.py`, `test_layer_consistency.py`, `test_capacity_coupling.py` |
| 6 | `model/solvers.py` | `test_solvers.py`, `test_avoidance.py` |
| 7 | `model/multilayer_graph.py`, `model/restoration.py` | `test_multilayer_graph.py`, `test_restoration.py` |
| 3 addendum | `model/topology_import.py` | `test_topology_import.py` |
| 8 | `model/whatif.py`, `model/network.py` (injection) | `test_whatif.py`, `test_injection_layer_consistency.py`, `test_server_phase6.py` |

---

## Suggested inspection order

Start at **Stage 1** to build a mental model of the data structures, then jump to **Stage 4** (the GNPy adapter) — that is where the most subtle physical assumptions live. Treat `test_layer_consistency.py` as the correctness oracle for the IP-optical coupling chain (Stage 5). After Stage 4, read the **Stage 3 addendum** (the Phase-6a importer that feeds synthesis), then **Stage 8** (Phase-6b injection composes failure + degradation over the Stage-4 synthesis path, so it depends on all of them being sound). Return to Stage 7 last since restoration correctness depends on all prior layers.
