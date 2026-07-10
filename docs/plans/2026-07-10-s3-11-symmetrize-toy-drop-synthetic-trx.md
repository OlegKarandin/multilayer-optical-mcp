# S3-11 — Symmetrize the toy & drop the `_synthetic_trx` branch (Option B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `_synthetic_trx` fallback from `synthesize._resolve_endpoint` so an OMS
endpoint must resolve to a `roadm_<id>` or an explicitly registered transceiver — otherwise
raise — turning a mistyped ROADM site into a loud error instead of a silently add/drop-OSNR-
less Transceiver (fixes Stage 3 point 4 / S3-4).

**Architecture (Option B, chosen by the user):** *Symmetrize* the toy line system — give it a
drop ROADM at the Z end — so every synthesis fixture terminates at a ROADM at both ends and
adopts the importer naming convention (`roadm_<id>` add-ROADM as OMS `chain[0]`, a registered
`trx_<id>` per endpoint, the drop ROADM resolved from the OMS `dst`). The C2 Step-B terminal-
ROADM propagation then applies the drop-side `add_drop_osnr` at Z. GSNR shifts (an extra drop
penalty) but **no test pins an absolute toy GSNR** — assertions are `isfinite`, `margin < 0`,
`fwd ≈ bwd`, and synthesized-vs-file equivalence — so the shift is absorbed as long as the file
and the synthesized model symmetrize together. A defensive guard on the transceiver-site
connection loop (skip when `roadm_<site>` is absent) hardens synthesis against dangling edges.

**Tech Stack:** Python, GNPy 2.14.0, pytest (`conda run -n multilayer-optical-mcp pytest`),
FastMCP.

## Global Constraints

- Run all tests via `conda run -n multilayer-optical-mcp python -m pytest`.
- **Gate:** `tests/gnpy_adapter/test_ground_truth_bridge.py::test_synthesized_toy_matches_file_loaded_toy`
  must stay green (`TOL_DB = 0.25`) — it compares synthesized vs file-loaded GSNR, both of
  which gain the Z drop ROADM in Task 1, so they track together.
- One commit per task (TDD: failing test → implementation → passing test → commit).
- The importer (`model_from_abstract_graph`) already builds ROADM-terminated OMS end-to-end
  (german_17), so the machinery this plan relies on exists and is tested.
- **Do not** alter `topologies/toy_2route.json` (loaded via `topo_path`, never synthesized) or
  any model-only fixture that never calls `compute_qot`/synthesis (`test_assets.py`,
  `test_network.py`, `test_rsa.py`) — dropping the synthetic branch does not affect them.

---

### Task 1: Symmetrize the toy (`toy_2span.json` + `_toy_model_synthesized`) — the gate

**Files:**
- Modify: `topologies/toy_2span.json` (insert a `ROADM Z` drop element + rewire the Z end)
- Modify: `tests/gnpy_adapter/test_ground_truth_bridge.py:28-57` (`_toy_model_synthesized`)
- Test: `tests/gnpy_adapter/test_ground_truth_bridge.py`

**Interfaces:**
- Consumes: `compute_qot(model, store, oms_sequence, direction, mode_id, loading, topo_path=None)`;
  the C2 Step-B terminal-ROADM append (`adapter.py:222-228`, `_roadm_successor`).
- Produces: a symmetric toy where the forward path ends `... → east edfa at Z → ROADM Z → trx Z`
  (file) and the synthesized model resolves its OMS `dst="Z"` to `roadm_Z`.

- [ ] **Step 1: Edit `toy_2span.json` to add the drop ROADM.** In `elements`, insert after the
  `east edfa at Z` element (before `trx Z`):

```json
{
  "uid": "ROADM Z",
  "type": "Roadm"
}
```

  In `connections`, replace the single `{"from_node": "east edfa at Z", "to_node": "trx Z"}`
  entry with two entries:

```json
{ "from_node": "east edfa at Z", "to_node": "ROADM Z" },
{ "from_node": "ROADM Z",        "to_node": "trx Z"   }
```

- [ ] **Step 2: Run the gate — expect the *comparison* to still hold but confirm the file loads.**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/gnpy_adapter/test_ground_truth_bridge.py::test_synthesized_toy_matches_file_loaded_toy -v`
Expected: FAIL — the file now has a Z drop ROADM (extra penalty) but `_toy_model_synthesized`
does not yet, so `g_syn` and `g_leg` diverge by more than `TOL_DB`. (If it errors on load
instead, the JSON edit is malformed — fix before continuing.)

- [ ] **Step 3: Symmetrize `_toy_model_synthesized`.** Replace the body of
  `_toy_model_synthesized()` (test_ground_truth_bridge.py:41-57) so it registers a drop ROADM
  at Z and registered transceivers, and its OMS resolves to ROADMs at both ends:

```python
    n = NetworkModel(modes=ModeRegistry([_mode()]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_roadm(ROADM(id="roadm_A"))
    n.add_roadm(ROADM(id="roadm_Z"))
    n.add_transceiver(Transceiver(id="trx_A", site="A"))
    n.add_transceiver(Transceiver(id="trx_Z", site="Z"))
    n.add_amplifier(Amplifier(id="amp_booster", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber_0", a_end="roadm_A", z_end="amp_ila",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp_ila", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber_1", a_end="amp_ila", z_end="amp_preamp",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp_preamp", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    # OMS endpoints are node ids that resolve to roadm_A (add, chain[0]) and
    # roadm_Z (drop, appended by C2 Step B via _roadm_successor).
    n.add_oms(OMS(id="oms_syn", src_node_id="A", dst_node_id="Z", elements=(
        "roadm_A", "amp_booster", "fiber_0", "amp_ila",
        "fiber_1", "amp_preamp")))
    return n
```

  Add `Transceiver` to the `assets` import at the top of the file
  (test_ground_truth_bridge.py:8-10): `... OMS, ROADM, Transceiver, TransceiverMode`.
  Update the docstring: the model now mirrors a *symmetric* toy_2span with a Z drop ROADM.

- [ ] **Step 4: Run the gate — expect PASS.**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/gnpy_adapter/test_ground_truth_bridge.py -v`
Expected: PASS — both `g_syn` and `g_leg` now include the Z drop penalty and agree within `TOL_DB`.

- [ ] **Step 5: Commit.**

```bash
git add topologies/toy_2span.json tests/gnpy_adapter/test_ground_truth_bridge.py
git commit -m "test(ground-truth): symmetrize toy with a Z drop ROADM (S3-11 Option B, task 1)"
```

---

### Task 2: Migrate `_toy_model` (test_compute_qot) to the importer convention

**Files:**
- Modify: `tests/gnpy_adapter/test_compute_qot.py` (`_toy_model`, its ROADM/trx names, OMS endpoints)
- Test: `tests/gnpy_adapter/test_compute_qot.py`, `tests/gnpy_adapter/test_per_direction.py`,
  `tests/gnpy_adapter/test_recompute_under_loading.py` (both import `_toy_model`)

**Interfaces:**
- Consumes: nothing new; same `compute_qot` signature.
- Produces: a `_toy_model` whose forward OMS `oms-AZ` (`src="A"`, `dst="Z"`) and reverse OMS
  `oms-ZA` (`src="Z"`, `dst="A"`) both resolve to `roadm_A`/`roadm_Z`, so forward gets
  add(`roadm_A`)+drop(`roadm_Z`) and backward gets add(`roadm_Z`)+drop(`roadm_A`) — symmetric,
  preserving the `abs(fwd - bwd) < 0.05` assertion in test_per_direction.

- [ ] **Step 1: Confirm the failure the migration must fix (baseline of the loud error).**
  This task has no new assertion of its own; it keeps existing tests green after Task 4 removes
  the synthetic branch. Run the current tests to record the green baseline:

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/gnpy_adapter/test_compute_qot.py tests/gnpy_adapter/test_per_direction.py tests/gnpy_adapter/test_recompute_under_loading.py -q`
Expected: PASS (baseline).

- [ ] **Step 2: Rewrite `_toy_model` to the importer convention.** In
  `tests/gnpy_adapter/test_compute_qot.py`, add `Transceiver` to the assets import, and change:
  - `ROADM(id="ROADM A", ...)` → `ROADM(id="roadm_A")`; `ROADM(id="ROADM Z", ...)` → `ROADM(id="roadm_Z")`.
  - Register two transceivers: `n.add_transceiver(Transceiver(id="trx_A", site="A"))` and
    `n.add_transceiver(Transceiver(id="trx_Z", site="Z"))`.
  - In the forward OMS `oms-AZ`: `src_node_id="A"`, `dst_node_id="Z"`, and change the first
    element from `"ROADM A"` to `"roadm_A"`.
  - In the reverse OMS `oms-ZA`: `src_node_id="Z"`, `dst_node_id="A"`, and change its first
    element from `"ROADM Z"` to `"roadm_Z"`.
  - Update the two fiber `a_end` values that referenced `"ROADM A"`/`"ROADM Z"` to
    `"roadm_A"`/`"roadm_Z"` (fiber endpoints are decorative for synthesis but keep them consistent).

  Leave amplifier and fiber UIDs unchanged (`booster A`, `east fiber A to ILA`, etc.) — endpoint
  resolution only inspects the `roadm_`/registered-`trx_` names.

- [ ] **Step 3: Run the three dependent test files — expect PASS (still synthetic branch present).**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/gnpy_adapter/test_compute_qot.py tests/gnpy_adapter/test_per_direction.py tests/gnpy_adapter/test_recompute_under_loading.py -q`
Expected: PASS. In particular `test_per_direction`'s `abs(fwd - bwd) < 0.05` still holds because
both directions now carry one add + one drop ROADM penalty.

- [ ] **Step 4: Commit.**

```bash
git add tests/gnpy_adapter/test_compute_qot.py
git commit -m "test(compute-qot): migrate _toy_model to importer convention (S3-11 Option B, task 2)"
```

---

### Task 3: Migrate `_seed_app` (test_server_phase4_rsa) and sweep for stragglers

**Files:**
- Modify: `tests/test_server_phase4_rsa.py:29-52` (`_seed_app`)
- Test: `tests/test_server_phase4_rsa.py` + a full-suite sweep

**Interfaces:**
- Consumes: `solve_allocation` (→ `compute_qot` → synthesis), `check_spectrum_feasibility`, `solve_rsa`.
- Produces: an in-model toy whose OMS endpoints resolve to `roadm_A`/`roadm_Z` and registered
  `trx_A`/`trx_Z`.

- [ ] **Step 1: Migrate `_seed_app`.** In `tests/test_server_phase4_rsa.py`, add `Transceiver`
  to the assets import, then in `_seed_app`:
  - `m.add_roadm(ROADM(id="ROADM A", ...))` → `m.add_roadm(ROADM(id="roadm_A"))`;
    `ROADM Z` → `roadm_Z`.
  - Register `m.add_transceiver(Transceiver(id="trx_A", site="A"))` and `trx_Z` (site `"Z"`).
  - `Fiber("east fiber A to ILA", "ROADM A", ...)` a_end → `"roadm_A"`;
    `Fiber("west fiber Z to ILA", "ROADM Z", ...)` a_end → `"roadm_Z"`.
  - `OMS("oms-AZ", "trx A", "trx Z", ("ROADM A", ...))` → `OMS("oms-AZ", "A", "Z", ("roadm_A", ...))`.
  - `OMS("oms-ZA", "trx Z", "trx A", ("ROADM Z", ...))` → `OMS("oms-ZA", "Z", "A", ("roadm_Z", ...))`.
  - Update any other seed helper in the file that builds `"ROADM A"`/`"trx A"`-style OMS the
    same way (there are 7 `trx` references; migrate each OMS/ROADM the same way).

- [ ] **Step 2: Run the server RSA suite — expect PASS (synthetic branch still present).**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/test_server_phase4_rsa.py -q`
Expected: PASS.

- [ ] **Step 3: Full-suite sweep for any remaining synthesis fixture with bare `trx` endpoints.**

Run: `conda run -n multilayer-optical-mcp python -m pytest -q 2>&1 | tail -5`
Expected: PASS (261 + this plan's deltas). This is the discovery step: the synthetic branch is
still in place, so nothing should fail yet. If any test synthesizes a bare-`trx` model not yet
migrated, it will be caught in Task 4 Step 3 (below) and migrated with the same recipe.

- [ ] **Step 4: Commit.**

```bash
git add tests/test_server_phase4_rsa.py
git commit -m "test(server-rsa): migrate _seed_app to importer convention (S3-11 Option B, task 3)"
```

---

### Task 4: Guard the site loop, drop the synthetic branch, assert the loud error (S3-4)

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py` (`model_to_gnpy_topology`,
  `_resolve_endpoint`, transceiver-site loop)
- Modify: `tests/gnpy_adapter/test_compute_qot.py:1-5` (update the ground-truth comment)
- Test: `tests/gnpy_adapter/test_synthesize.py` (new loud-error test)

**Interfaces:**
- Consumes: `NetworkModel._roadms`, `NetworkModel._transceivers`.
- Produces: `_resolve_endpoint(node_id)` returning `roadm_<id>` or a registered transceiver id,
  else raising `ValueError`; a transceiver-site loop that skips a transceiver whose
  `roadm_<site>` is not registered.

- [ ] **Step 1: Write the failing loud-error test.** Append to `tests/gnpy_adapter/test_synthesize.py`:

```python
def test_unresolvable_oms_endpoint_raises():
    """S3-4: a mistyped OMS endpoint (neither roadm_<id> nor a registered
    transceiver) must raise, not silently synthesize a penalty-free Transceiver."""
    import pytest
    from multilayer_optical_mcp.model.assets import OMS
    model = _model_with(roadm=ROADM(id="roadm_A"),
                        amp=Amplifier(id="amp_x", type_variety="advanced_toy",
                                      gain_db=20.0, nf_db=5.5))
    # OMS whose dst is a bare, unregistered site id.
    model.add_oms(OMS(id="oms_bad", src_node_id="A", dst_node_id="typo_Z",
                      elements=("roadm_A", "amp_x")))
    with pytest.raises(ValueError):
        model_to_gnpy_topology(model)
```

- [ ] **Step 2: Run it — expect FAIL.**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/gnpy_adapter/test_synthesize.py::test_unresolvable_oms_endpoint_raises -v`
Expected: FAIL — today `_resolve_endpoint` synthesizes `typo_Z` as a Transceiver, no raise.

- [ ] **Step 3: Implement — guard the site loop and drop the synthetic branch.** In
  `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py`:

  Replace `_resolve_endpoint` (drop the third branch and the `_synthetic_trx` set):

```python
    def _resolve_endpoint(node_id: str) -> str:
        """Return the GNPy UID for an OMS endpoint (src or dst).

        S3-11: an endpoint must be either a `roadm_<node_id>` ROADM or an
        explicitly registered transceiver. Anything else (a mistyped ROADM
        site) is a modelling error and raises, rather than being silently
        demoted to a penalty-free synthetic Transceiver (S3-4)."""
        roadm_uid = f"roadm_{node_id}"
        if roadm_uid in model._roadms:
            return roadm_uid
        if node_id in model._transceivers:
            return node_id
        raise ValueError(
            f"OMS endpoint {node_id!r} resolves to neither a registered ROADM "
            f"({roadm_uid!r}) nor a registered transceiver; register one or fix "
            f"the OMS endpoint id"
        )
```

  Remove the `_synthetic_trx: set = set()` line and the deferred flush loop
  (`for uid in sorted(_synthetic_trx): elements.append(...)`).

  Guard the transceiver-site connection loop so a terminal transceiver whose site has no
  co-located ROADM does not emit a dangling connection:

```python
    for t in model._transceivers.values():
        roadm_uid = f"roadm_{t.site}"
        if roadm_uid not in model._roadms:
            continue  # line-terminal transceiver with no co-located ROADM
        connect(t.id, roadm_uid)
        connect(roadm_uid, t.id)
```

- [ ] **Step 4: Run the loud-error test + the synthesis suite — expect PASS.**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/gnpy_adapter/test_synthesize.py -q`
Expected: PASS (including the new loud-error test).

- [ ] **Step 5: Update the ground-truth comment.** In `tests/gnpy_adapter/test_compute_qot.py:1-5`,
  update the header comment: the topology now terminates at a drop ROADM at Z, so the forward
  cascade carries a second `add_drop_osnr` penalty. Replace the stale
  `fwd ~18.85, bwd ~17.53` values with the freshly observed values (read them from a one-off run
  of `_gsnr_synthesized`/`_gsnr_legacy` or from `compute_qot` on `_toy_model`), and note the Z
  drop ROADM in the topology line. This is documentation only — no assertion depends on it.

- [ ] **Step 6: Run the FULL suite — expect PASS (this is the definitive discovery gate).**

Run: `conda run -n multilayer-optical-mcp python -m pytest -q 2>&1 | tail -6`
Expected: PASS. If any test now fails with the new `ValueError` from `_resolve_endpoint`, it is
a straggler synthesis fixture with a bare `trx` endpoint — migrate it with the Task 2/3 recipe
(rename its ROADM to `roadm_<id>`, register `trx_<id>`, set OMS endpoints to node ids), then
re-run. Repeat until green.

- [ ] **Step 7: Commit.**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/synthesize.py tests/gnpy_adapter/test_synthesize.py tests/gnpy_adapter/test_compute_qot.py
git commit -m "fix(synthesize): drop _synthetic_trx, raise on unresolvable OMS endpoint (Batch C7 S3-11, S3-4)"
```

---

### Task 5: Update the roadmap & fix-plan docs

**Files:**
- Modify: `docs/inspection-roadmap.md` (Stage 3 point 11 + point 4 → RESOLVED)
- Modify: `docs/plans/2026-07-07-fix-roadmap-correctness-then-optimizations.md` (Batch C7 status)

- [ ] **Step 1: Mark S3-11 and S3-4 RESOLVED in `inspection-roadmap.md`.** Replace the
  "DEFERRED in Batch C7" note under Stage 3 point 11 with a "RESOLVED (Option B)" note recording:
  the toy gained a Z drop ROADM; the three synthesis fixtures adopted the importer convention;
  the site loop is guarded; `_resolve_endpoint` now raises on an unresolvable endpoint (S3-4
  fixed for free); GSNR shifted (extra Z drop penalty) but no absolute value is pinned and the
  syn-vs-file gate holds.

- [ ] **Step 2: Update Batch C7 status in the fix plan.** Mark item 2 (S3-11) ✅ DONE and remove
  the C7-DEFER block (or convert it to a "landed via `2026-07-10-...` plan" pointer).

- [ ] **Step 3: Commit.**

```bash
git add docs/inspection-roadmap.md docs/plans/2026-07-07-fix-roadmap-correctness-then-optimizations.md
git commit -m "docs: record S3-11 (Option B) resolution (Batch C7)"
```

---

## Self-Review notes

- **Spec coverage:** S3-11 (drop synthetic branch) → Task 4; S3-4 (loud error) → Task 4 Step 1/3;
  symmetrize toy → Task 1; fixture migrations → Tasks 1–3; discovery of stragglers → Task 3 Step 3
  + Task 4 Step 6; docs → Task 5.
- **Blast radius (verified):** synthesis fixtures with bare-`trx` endpoints are `_toy_model_synthesized`
  (Task 1), `_toy_model` (Task 2), `_seed_app` (Task 3). Model-only fixtures (`test_assets`,
  `test_network`, `test_rsa`), `test_translate._toy_model_oms` (resolver-only), and importer-based
  adapter tests (`test_terminal_roadm`, `test_probe_frequency`, `test_reverse_oms`, `test_synthesize`)
  never hit `_resolve_endpoint`'s synthetic branch. Task 4 Step 6 is the safety net for any missed one.
- **Type consistency:** `Transceiver(id, site)`, `ROADM(id, target_pch_out_db=-20.0)`,
  `OMS(id, src_node_id, dst_node_id, elements)` match `assets.py`. `_resolve_endpoint` returns `str`
  or raises `ValueError`. `model._roadms`/`model._transceivers` are the dicts used elsewhere in
  `model_to_gnpy_topology`.
- **Guard rationale:** with full importer naming every `trx_<site>` has a matching `roadm_<site>`,
  so the guard is defensive (never triggers for the migrated fixtures) — but it prevents a future
  registered line-terminal transceiver from emitting a dangling connection, and it is required if a
  fixture ever registers a transceiver whose site has no ROADM.
