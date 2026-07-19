# multilayer-optical-mcp — Phase 6b: What-if + injection (branch-scoped)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for every function. Steps use checkbox (`- [ ]`) syntax for tracking. **Depends on Phase 6a** (`2026-06-06-phase-6a-topology-synthesis.md`) being merged: this plan assumes GNPy is synthesized from the model so that perturbing `Amplifier.nf_db` / fiber loss on a branch and re-synthesizing actually changes the propagated GSNR.

**Goal:** Land Build-order **Step 6 (what-if + injection)** — three branch-scoped tools that reason about fragility and physical degradation without ever mutating ground truth: `whatif_margin_threshold_sweep(threshold_db)` (physics-free screening), `inject_degradation(asset, {nf, loss})` (perturb the impairment → recompute → typed threshold crossings), and `inject_failure(asset_set)` (mark assets failed → affected lightpaths down, capacity 0). Margin is an **output**, never an input.

**Architecture:** Injection is a branch mutation of the *model's own physical parameters* (`Amplifier.nf_db`, a new `Fiber.extra_loss_db`) plus a `failed_assets` set, all carried by the snapshot clone/diff. Because Phase 6a rebuilds the GNPy network from the model each evaluation, `inject_degradation` perturbs the parameter and calls the existing `recompute_qot_under_loading`; the GSNR moves as a physical consequence. `inject_failure` is physics-free: a cut fiber/dead amp means the lightpaths crossing it are down, so their QoT is set to a failed sentinel (margin = −∞), which the already-built margin-gated `ip_link_capacity_gbps` turns into capacity 0 and `simulate_ip_routing` turns into dropped traffic. The threshold sweep is a pure read over recorded QoT state.

**Tech Stack:** Python 3.11+, gnpy==2.11.1, NetworkX ≥3.2, pytest, FastMCP. No new deps.

---

## Context (what already exists — do not rebuild)

- **Phase 6a delivered model-sourced physics.** `compute_qot`/`recompute_qot_under_loading` build the GNPy network from the model via `gnpy_adapter/synthesize.build_gnpy_network(model)`. `Amplifier.nf_db` is the GNPy NF (one advanced-model `type_variety` per distinct NF). **This is what makes `inject_degradation` work — without 6a a bare NF delta would no-op.**
- **`recompute_qot_under_loading(model, store, loading)`** (adapter.py:343) computes gated (worse-direction) QoT for *every* lightpath under a given loading, writes `QoTState` onto the model via `set_qot_state`, and returns `{lp_id: (QoTState, result_id)}`. Reuse it verbatim — injection perturbs a parameter and calls this.
- **Margin-gated capacity is DONE.** `NetworkModel.ip_link_capacity_gbps(link_id)` returns `0.0` when the bound lightpath's `margin_db < 0`, else `mode.bitrate_gbps`. A failed/degraded lightpath whose margin goes negative therefore yields capacity 0 with no extra code (CLAUDE.md margin-feasibility gate).
- **`simulate_ip_routing(model)`** (model/ip_routing.py, exposed at server.py:217) already reports `down` links (capacity 0) and the services crossing them as dropped. Injection only has to drive capacity to 0; the IP consequence is already modelled.
- **Asset-crossing helper exists:** `model/exposure.oms_seq_asset_set(model, oms_sequence)` → `{oms_ids} ∪ {fiber/amp/roadm uids}`. A lightpath crosses a failed asset iff `oms_seq_asset_set(model, lp.oms_sequence) & failed_assets`.
- **Snapshot clone/diff** (`model/snapshots.py`): `_clone` copies every `_*` dict; `diff` reports added/removed/modified per registry. Any new model field MUST be added to both `_clone` and `diff` or branches silently share state / diffs miss it.
- **Server pattern:** every tool reads `snapshots.current()`; serializers live in `model/views.py` and are imported inside `build_app`. Injection tools operate on `snapshots.current()`, which the agent is expected to have set to a **branch** via `snapshot_branch` (same contract as `reroute_service`/`set_modulation_format`).
- **Tool-name contract.** The CLAUDE.md Tool surface is authoritative: the screening tool is **`whatif_margin_threshold_sweep(threshold_db)`**. (Build-order step 6 calls it `whatif_margin_delta` — that is a stale alias; use the Tool-surface name.)

---

## Decisions settled (call out at spec review if you disagree)

1. **Injection mutates the model's physical parameters in place on the branch — there is no separate "overlay" store.** `inject_degradation(nf:+x)` does `Amplifier.nf_db += x`; `inject_degradation(loss:+y)` does `Fiber.extra_loss_db += y`. This keeps the model the single source of truth (6a), makes the change visible in `snapshot_diff` (the amp/fiber registries change), and reaps with the branch. Rejected: a parallel `{asset: delta}` overlay — it reintroduces a second source of truth for physics, exactly what 6a removed.
2. **`Fiber` gains `extra_loss_db` (default 0.0); the synthesizer maps it to GNPy fiber `att_in`.** Loss injection is lumped attenuation at the fiber input, which is how GNPy models a connector/splice/bend impairment. Span length and `loss_coef` are unchanged (those are physical constants); injected loss is additive on top.
3. **`inject_failure` is physics-free.** A failed fiber (cut) or amp (dead) does not need GNPy propagation — there is no signal. Affected lightpaths (those whose OMS asset set intersects the failed set) get a **failed sentinel** `QoTState(gsnr_db=-inf, osnr_db=-inf, margin_db=-inf, limiting_element_id=<first failed asset on path>)`. Capacity falls to 0 via the existing margin gate; `simulate_ip_routing` reports the drop. Lightpaths not crossing a failed asset are untouched.
4. **`inject_degradation` reports typed threshold crossings, never prose.** It records each lightpath's `margin_db` before, applies the perturbation, calls `recompute_qot_under_loading` with the model's current loading, then returns a `DegradationReport`: per-lightpath `{lp_id, margin_before, margin_after, feasible_before, feasible_after, crossed}` where `crossed = feasible_before and not feasible_after` (the feasible→infeasible flip is the load-bearing event). An optional `threshold_db` also flags lightpaths whose `margin_after` fell to within `threshold_db` of 0.
5. **The model's "current loading" is derived, one channel per lightpath.** `loading_from_model(model)` builds a `LoadingState` with one `Channel` per lightpath at its `center_freq_hz`, `slot_width_hz = mode.channel_spacing_hz`, `power_dbm = 0.0`, `mode_id = lp.mode_id`. This is the loading `inject_degradation` recomputes against (the committed channel set), distinct from the *constructed* loadings used for make-before-break what-ifs in step 7.
6. **`whatif_margin_threshold_sweep(threshold_db)` is a pure read and models no degradation.** It returns every lightpath whose recorded `margin_db <= threshold_db` (low margin = fragile; negatives are included as already-infeasible), sorted ascending by margin. It does **not** recompute and makes no causal claim — it is triage over whatever QoT state is currently recorded. Lightpaths with no recorded QoT state are omitted (nothing to screen).

---

## File structure

- **Create `src/multilayer_optical_mcp/model/whatif.py`** — the what-if/injection logic that changes together: `loading_from_model`, the `DegradationReport`/`FailureReport`/`MarginSweepRow` result types, `inject_degradation`, `inject_failure`, `margin_threshold_sweep`. One responsibility: *perturb physical parameters on a branch and report the QoT/feasibility consequences.*
- **Modify `src/multilayer_optical_mcp/model/assets.py`** — add `Fiber.extra_loss_db: float = 0.0`.
- **Modify `src/multilayer_optical_mcp/model/network.py`** — add `_failed_assets: set[str]` + `mark_failed`, `clear_failed`, `failed_assets`, `is_failed`; add `apply_nf_delta(amp_id, delta)` and `apply_loss_delta(fiber_id, delta)` mutators (so `inject_degradation` does not reach into private dicts).
- **Modify `src/multilayer_optical_mcp/model/snapshots.py`** — `_clone` copies `_failed_assets`; `diff` reports `failed_assets`.
- **Modify `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py`** — fiber `att_in` reads `f.extra_loss_db` instead of literal `0`.
- **Modify `src/multilayer_optical_mcp/model/views.py`** — `degradation_report_dict`, `failure_report_dict`, `margin_sweep_dict`.
- **Modify `src/multilayer_optical_mcp/server.py`** — expose `whatif_margin_threshold_sweep`, `inject_degradation`, `inject_failure`.
- **Create `tests/model/test_whatif.py`** — sweep, inject_degradation crossing, inject_failure sentinel, loading_from_model.
- **Create `tests/model/test_injection_layer_consistency.py`** — the headline branch proofs: NF injection lowers margin and shrinks IP capacity; failure zeroes capacity and drops the service; ground truth untouched.
- **Create `tests/test_server_phase6.py`** — the three tools end-to-end through FastMCP on a branch.

---

# Part A — model plumbing (no GNPy, no tools)

## Task 1: `Fiber.extra_loss_db` + synthesizer maps it to `att_in`

**Files:**
- Modify: `src/multilayer_optical_mcp/model/assets.py`
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py`
- Test: `tests/model/test_whatif.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/model/test_whatif.py
import math
from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import Direction
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph

MODE = "400G@7.1dB"


def _reg():
    return ModeRegistry([TransceiverMode(id=MODE, bitrate_gbps=400.0,
        required_gsnr_db=7.1, symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)])


def _one_edge_model(extra_loss_db: float = 0.0):
    m = model_from_abstract_graph({
        "nodes": [{"id": 0}, {"id": 1}],
        "edges": [{"src": 0, "dst": 1, "length_km": 160.0, "num_spans": 2,
                   "span_lengths_km": [80.0, 80.0], "fiber_type": "SSMF",
                   "amplifier_nf_db": [5.5, 5.5]}],
    }, modes=_reg())
    if extra_loss_db:
        m.apply_loss_delta("fiber_0_1_0", extra_loss_db)
    return m


def _gsnr(m):
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, MODE),))
    st, _ = compute_qot(model=m, store=store, oms_sequence=("oms_0_1",),
                        direction=Direction.FORWARD, mode_id=MODE, loading=loading)
    return st.gsnr_db


def test_extra_loss_lowers_gsnr():
    base = _gsnr(_one_edge_model(0.0))
    degraded = _gsnr(_one_edge_model(4.0))
    assert degraded < base - 0.5  # 4 dB lumped loss is visible
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/model/test_whatif.py::test_extra_loss_lowers_gsnr -v`
Expected: FAIL (`apply_loss_delta` undefined — added in Task 2; for now `AttributeError`).

> Tasks 1 and 2 are interdependent (the test needs `apply_loss_delta`). Implement Task 1's dataclass + synthesizer change AND Task 2's mutators, then run this test. Keep them as separate commits.

- [ ] **Step 3: Add the field**

In `model/assets.py`, change `Fiber`:

```python
@dataclass(frozen=True)
class Fiber:
    id: str
    a_end: str
    z_end: str
    length_km: float
    type_variety: str
    extra_loss_db: float = 0.0
```

- [ ] **Step 4: Map it in the synthesizer.** In `gnpy_adapter/synthesize.py:model_to_gnpy_topology`, change the fiber `att_in`:

```python
        elements.append({"uid": f.id, "type": "Fiber", "type_variety": f.type_variety,
                         "params": {"length": f.length_km, "length_units": "km",
                                    "loss_coef": loss, "att_in": f.extra_loss_db,
                                    "con_in": 0, "con_out": 0}})
```

- [ ] **Step 5: Commit (after Task 2 makes the test pass)**

```bash
git add src/multilayer_optical_mcp/model/assets.py src/multilayer_optical_mcp/gnpy_adapter/synthesize.py
git commit -m "feat(model): Fiber.extra_loss_db; synthesizer maps it to GNPy att_in"
```

---

## Task 2: `failed_assets` + mutators on NetworkModel; snapshot clone/diff

**Files:**
- Modify: `src/multilayer_optical_mcp/model/network.py`
- Modify: `src/multilayer_optical_mcp/model/snapshots.py`
- Test: `tests/model/test_whatif.py`, `tests/model/test_snapshots.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/model/test_whatif.py
def test_mark_failed_and_query():
    m = _one_edge_model()
    m.mark_failed(("fiber_0_1_0",))
    assert m.is_failed("fiber_0_1_0")
    assert not m.is_failed("fiber_0_1_1")
    assert m.failed_assets() == frozenset({"fiber_0_1_0"})


def test_apply_nf_delta_mutates_amp():
    m = _one_edge_model()
    before = m.get_amplifier("amp_0_1_0").nf_db
    m.apply_nf_delta("amp_0_1_0", 3.0)
    assert m.get_amplifier("amp_0_1_0").nf_db == before + 3.0


def test_failed_assets_isolated_on_branch(tmp_path):
    # branch isolation: marking failed on a branch must not touch the parent.
    from multilayer_optical_mcp.model.snapshots import SnapshotStore
    base = _one_edge_model()
    store = SnapshotStore(base)
    sid = store.create()
    bid = store.branch(sid)
    store.current().mark_failed(("fiber_0_1_0",))
    assert store.current().is_failed("fiber_0_1_0")
    assert not store.get(sid).is_failed("fiber_0_1_0")  # parent untouched
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/model/test_whatif.py -k "failed or nf_delta" -v`
Expected: FAIL (`mark_failed`/`apply_nf_delta` undefined).

- [ ] **Step 3: Implement on NetworkModel.** Add to `__init__`: `self._failed_assets: set[str] = set()`. Add methods (place near the QoT/mode mutation section):

```python
    # ---------------------------------------------------------------- injection mutators

    def apply_nf_delta(self, amp_id: str, delta_db: float) -> None:
        """Add delta_db to an amplifier's NF (branch what-if). Raises KeyError if unknown."""
        amp = self._amplifiers[amp_id]
        self._amplifiers[amp_id] = replace(amp, nf_db=amp.nf_db + delta_db)

    def apply_loss_delta(self, fiber_id: str, delta_db: float) -> None:
        """Add delta_db of lumped loss to a fiber (branch what-if). Raises KeyError if unknown."""
        f = self._fibers[fiber_id]
        self._fibers[fiber_id] = replace(f, extra_loss_db=f.extra_loss_db + delta_db)

    def mark_failed(self, asset_ids: Tuple[str, ...]) -> None:
        """Mark assets failed on this (branch) model."""
        self._failed_assets.update(asset_ids)

    def clear_failed(self, asset_ids: Tuple[str, ...] = ()) -> None:
        """Clear specific failed assets, or all when asset_ids is empty."""
        if asset_ids:
            self._failed_assets.difference_update(asset_ids)
        else:
            self._failed_assets.clear()

    def failed_assets(self) -> frozenset:
        return frozenset(self._failed_assets)

    def is_failed(self, asset_id: str) -> bool:
        return asset_id in self._failed_assets
```

- [ ] **Step 4: Carry it through snapshots.** In `model/snapshots.py:_clone`, add after `clone._qot_state = dict(m._qot_state)`:

```python
        clone._failed_assets = set(m._failed_assets)
```

and in `diff`, add to the returned dict:

```python
            "failed_assets": _delta_set(a._failed_assets, b._failed_assets),
```

with a small set-delta helper at the bottom of the file (the existing `_delta` is dict-only):

```python
def _delta_set(a: set, b: set) -> dict:
    return {
        "added": tuple(sorted(b - a)),
        "removed": tuple(sorted(a - b)),
        "modified": (),
    }
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/model/test_whatif.py -k "failed or nf_delta or extra_loss" -v && pytest tests/model/test_snapshots.py -v`
Expected: PASS. Now also commit Task 1.

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py src/multilayer_optical_mcp/model/snapshots.py tests/model/test_whatif.py
git commit -m "feat(model): failed_assets + nf/loss mutators; snapshot clone/diff carry them"
```

---

# Part B — what-if + injection logic

## Task 3: `loading_from_model` + `margin_threshold_sweep`

**Files:**
- Create: `src/multilayer_optical_mcp/model/whatif.py`
- Test: `tests/model/test_whatif.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/model/test_whatif.py
from multilayer_optical_mcp.model.whatif import (
    loading_from_model, margin_threshold_sweep, MarginSweepRow,
)
from multilayer_optical_mcp.model.assets import OMS, Lightpath
from multilayer_optical_mcp.model.qot import QoTState


def _model_with_two_lightpaths():
    m = _one_edge_model()
    m.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_0_1",),
                              mode_id=MODE, center_freq_hz=193.4e12))
    m.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms_1_0",),
                              mode_id=MODE, center_freq_hz=193.5e12))
    m.set_qot_state("lp0", QoTState(gsnr_db=9.0, osnr_db=20.0, margin_db=1.9))
    m.set_qot_state("lp1", QoTState(gsnr_db=8.0, osnr_db=19.0, margin_db=0.9))
    return m


def test_loading_from_model_one_channel_per_lightpath():
    m = _model_with_two_lightpaths()
    loading = loading_from_model(m)
    assert len(loading.channels) == 2
    freqs = sorted(c.center_freq_hz for c in loading.channels)
    assert freqs == [193.4e12, 193.5e12]


def test_sweep_returns_fragile_sorted_by_margin():
    m = _model_with_two_lightpaths()
    rows = margin_threshold_sweep(m, threshold_db=2.0)
    assert [r.lightpath_id for r in rows] == ["lp1", "lp0"]  # ascending margin
    assert all(isinstance(r, MarginSweepRow) for r in rows)


def test_sweep_excludes_well_margined():
    m = _model_with_two_lightpaths()
    rows = margin_threshold_sweep(m, threshold_db=1.0)
    assert [r.lightpath_id for r in rows] == ["lp1"]  # lp0 margin 1.9 > 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/model/test_whatif.py -k "loading_from_model or sweep" -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# src/multilayer_optical_mcp/model/whatif.py
"""Branch-scoped what-if + injection.

Margin is an OUTPUT: the sweep screens recorded margin; inject_degradation
perturbs a physical parameter and lets margin move via recompute. None of these
mutate ground truth — callers pass a branch model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from ..gnpy_adapter.loading import Channel, LoadingState
from .network import NetworkModel
from .qot import QoTState
from .qot_results import QoTResultStore


@dataclass(frozen=True)
class MarginSweepRow:
    lightpath_id: str
    margin_db: float
    gsnr_db: float
    mode_feasible: bool


def loading_from_model(model: NetworkModel) -> LoadingState:
    """One channel per committed lightpath at its center frequency."""
    channels = []
    for lp in model.list_lightpaths():
        mode = model.modes.get(lp.mode_id)
        channels.append(Channel(
            center_freq_hz=lp.center_freq_hz,
            slot_width_hz=mode.channel_spacing_hz,
            power_dbm=0.0,
            mode_id=lp.mode_id,
        ))
    return LoadingState(channels=tuple(channels))


def margin_threshold_sweep(model: NetworkModel, threshold_db: float) -> List[MarginSweepRow]:
    """Pure read: lightpaths whose recorded margin_db <= threshold_db, ascending.

    Physics-free screening (CLAUDE.md). Lightpaths with no recorded QoT are omitted.
    """
    rows: List[MarginSweepRow] = []
    for lp in model.list_lightpaths():
        try:
            st = model.get_qot_state(lp.id)
        except LookupError:
            continue
        if st.margin_db <= threshold_db:
            rows.append(MarginSweepRow(lightpath_id=lp.id, margin_db=st.margin_db,
                                       gsnr_db=st.gsnr_db, mode_feasible=st.mode_feasible))
    rows.sort(key=lambda r: r.margin_db)
    return rows
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/model/test_whatif.py -k "loading_from_model or sweep" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/whatif.py tests/model/test_whatif.py
git commit -m "feat(whatif): loading_from_model + physics-free margin_threshold_sweep"
```

---

## Task 4: `inject_failure`

**Files:**
- Modify: `src/multilayer_optical_mcp/model/whatif.py`
- Test: `tests/model/test_whatif.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/model/test_whatif.py
import math as _math
from multilayer_optical_mcp.model.whatif import inject_failure, FailureReport


def test_inject_failure_downs_crossing_lightpath():
    m = _model_with_two_lightpaths()  # lp0 rides oms_0_1, lp1 rides oms_1_0
    report = inject_failure(m, ("fiber_0_1_0",))  # a fiber on oms_0_1 only
    # lp0 crosses the failed fiber -> sentinel; lp1 does not -> untouched
    assert _math.isinf(m.get_qot_state("lp0").margin_db)
    assert m.get_qot_state("lp0").margin_db < 0
    assert m.get_qot_state("lp1").margin_db == 0.9
    assert "lp0" in report.downed_lightpaths
    assert "lp1" not in report.downed_lightpaths


def test_inject_failure_records_failed_assets():
    m = _model_with_two_lightpaths()
    inject_failure(m, ("fiber_0_1_0",))
    assert m.is_failed("fiber_0_1_0")
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/model/test_whatif.py -k inject_failure -v`
Expected: FAIL (`inject_failure` undefined).

- [ ] **Step 3: Implement**

```python
# append to src/multilayer_optical_mcp/model/whatif.py
from .exposure import oms_seq_asset_set

_FAILED_SENTINEL = QoTState(gsnr_db=float("-inf"), osnr_db=float("-inf"),
                            margin_db=float("-inf"), limiting_element_id=None)


@dataclass(frozen=True)
class FailureReport:
    failed_assets: Tuple[str, ...]
    downed_lightpaths: Tuple[str, ...]


def inject_failure(model: NetworkModel, asset_ids: Tuple[str, ...]) -> FailureReport:
    """Mark assets failed on the (branch) model and down every lightpath crossing them.

    Physics-free: a cut fiber / dead amp carries no signal, so crossing lightpaths
    get a failed QoT sentinel (margin = -inf). Capacity falls to 0 via the existing
    margin gate; simulate_ip_routing reports the drop.
    """
    model.mark_failed(tuple(asset_ids))
    failed = set(asset_ids)
    downed: List[str] = []
    for lp in model.list_lightpaths():
        if oms_seq_asset_set(model, lp.oms_sequence) & failed:
            first = next((a for a in lp.oms_sequence_assets(model)
                          if a in failed), None) if hasattr(lp, "oms_sequence_assets") else None
            model.set_qot_state(lp.id, _FAILED_SENTINEL)
            downed.append(lp.id)
    return FailureReport(failed_assets=tuple(asset_ids), downed_lightpaths=tuple(downed))
```

> The `first`/`hasattr` line is defensive dead-weight — remove it and just call `model.set_qot_state(lp.id, _FAILED_SENTINEL)`. The sentinel's `limiting_element_id=None` is fine; if you want the failed asset surfaced, set `limiting_element_id` to `next(iter(oms_seq_asset_set(model, lp.oms_sequence) & failed))`. Keep it simple:

```python
    for lp in model.list_lightpaths():
        crossing = oms_seq_asset_set(model, lp.oms_sequence) & failed
        if crossing:
            model.set_qot_state(lp.id, QoTState(
                gsnr_db=float("-inf"), osnr_db=float("-inf"), margin_db=float("-inf"),
                limiting_element_id=sorted(crossing)[0]))
            downed.append(lp.id)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/model/test_whatif.py -k inject_failure -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/whatif.py tests/model/test_whatif.py
git commit -m "feat(whatif): inject_failure downs crossing lightpaths (margin -inf sentinel)"
```

---

## Task 5: `inject_degradation` + `DegradationReport`

**Files:**
- Modify: `src/multilayer_optical_mcp/model/whatif.py`
- Test: `tests/model/test_whatif.py`

`inject_degradation` records margins before, applies the NF/loss delta to the named asset, recomputes all lightpaths under `loading_from_model`, and reports per-lightpath crossings.

- [ ] **Step 1: Write the failing test** (uses real GNPy via recompute; build a real synthesizable model with a lightpath)

```python
# append to tests/model/test_whatif.py
from multilayer_optical_mcp.model.whatif import inject_degradation, DegradationReport


def _live_model_one_lightpath():
    """A model whose lp0 has real (synthesized) QoT, near threshold."""
    m = _one_edge_model()
    m.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_0_1",),
                              mode_id=MODE, center_freq_hz=193.4e12))
    # seed real QoT
    from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading
    recompute_qot_under_loading(model=m, store=QoTResultStore(),
                                loading=loading_from_model(m))
    return m


def test_inject_degradation_lowers_margin_and_reports():
    m = _live_model_one_lightpath()
    before = m.get_qot_state("lp0").margin_db
    report = inject_degradation(m, store=QoTResultStore(), asset_id="amp_0_1_0",
                                nf_delta=6.0, loss_delta=0.0)
    after = m.get_qot_state("lp0").margin_db
    assert after < before  # +6 dB NF degrades GSNR -> lower margin
    row = next(r for r in report.rows if r.lightpath_id == "lp0")
    assert row.margin_before == before
    assert row.margin_after == after
    assert isinstance(report, DegradationReport)


def test_inject_degradation_unknown_asset_raises():
    m = _live_model_one_lightpath()
    import pytest
    with pytest.raises(KeyError):
        inject_degradation(m, store=QoTResultStore(), asset_id="nope", nf_delta=1.0)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/model/test_whatif.py -k inject_degradation -v`
Expected: FAIL (`inject_degradation` undefined).

- [ ] **Step 3: Implement**

```python
# append to src/multilayer_optical_mcp/model/whatif.py
from ..gnpy_adapter.adapter import recompute_qot_under_loading


@dataclass(frozen=True)
class DegradationRow:
    lightpath_id: str
    margin_before: float
    margin_after: float
    feasible_before: bool
    feasible_after: bool
    crossed: bool            # feasible_before and not feasible_after
    within_threshold: bool   # margin_after <= threshold_db (when threshold given)


@dataclass(frozen=True)
class DegradationReport:
    asset_id: str
    nf_delta: float
    loss_delta: float
    rows: Tuple[DegradationRow, ...]
    crossings: Tuple[str, ...]  # lightpath ids that flipped feasible -> infeasible


def inject_degradation(
    model: NetworkModel,
    *,
    store: QoTResultStore,
    asset_id: str,
    nf_delta: float = 0.0,
    loss_delta: float = 0.0,
    threshold_db: float = 0.0,
) -> DegradationReport:
    """Perturb impairment on a branch, recompute, report threshold crossings.

    asset_id must be a known amplifier (for nf_delta) or fiber (for loss_delta).
    Raises KeyError on an unknown asset. Margin moves as a consequence — never set.
    """
    before = {lp.id: model.get_qot_state(lp.id).margin_db
              for lp in model.list_lightpaths()
              if _has_qot(model, lp.id)}

    if nf_delta:
        model.apply_nf_delta(asset_id, nf_delta)   # KeyError if not an amp
    if loss_delta:
        model.apply_loss_delta(asset_id, loss_delta)  # KeyError if not a fiber
    if not nf_delta and not loss_delta:
        raise ValueError("inject_degradation needs a non-zero nf_delta or loss_delta")

    recompute_qot_under_loading(model=model, store=store, loading=loading_from_model(model))

    rows: List[DegradationRow] = []
    crossings: List[str] = []
    for lp in model.list_lightpaths():
        st = model.get_qot_state(lp.id)
        mb = before.get(lp.id, float("inf"))
        fb = mb >= 0
        fa = st.mode_feasible
        crossed = fb and not fa
        if crossed:
            crossings.append(lp.id)
        rows.append(DegradationRow(
            lightpath_id=lp.id, margin_before=mb, margin_after=st.margin_db,
            feasible_before=fb, feasible_after=fa, crossed=crossed,
            within_threshold=st.margin_db <= threshold_db))
    return DegradationReport(asset_id=asset_id, nf_delta=nf_delta, loss_delta=loss_delta,
                             rows=tuple(rows), crossings=tuple(crossings))


def _has_qot(model: NetworkModel, lp_id: str) -> bool:
    try:
        model.get_qot_state(lp_id)
        return True
    except LookupError:
        return False
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/model/test_whatif.py -k inject_degradation -v`
Expected: PASS (2 tests). The NF+6 dB must lower margin; if it does not, the synthesizer is not honouring per-amp NF — fix Phase 6a, not this test.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/whatif.py tests/model/test_whatif.py
git commit -m "feat(whatif): inject_degradation perturbs NF/loss, recomputes, reports crossings"
```

---

# Part C — layer-consistency proof + server tools

## Task 6: Layer-consistency / branch-isolation proofs

**Files:**
- Create: `tests/model/test_injection_layer_consistency.py`

This is the headline: an injected physical change propagates up to IP capacity and congestion, on a branch, with ground truth untouched. Build a small IP-over-optical model (router pair, one IP link bound to a lightpath, a service), mirroring `tests/model/test_layer_consistency.py` from phase 5.

- [ ] **Step 1: Write the tests**

```python
# tests/model/test_injection_layer_consistency.py
import math
from multilayer_optical_mcp.model.assets import (
    IPLink, Lightpath, Router, Service,
)
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.snapshots import SnapshotStore
from multilayer_optical_mcp.model.whatif import (
    inject_failure, inject_degradation, loading_from_model,
)
from multilayer_optical_mcp.model.ip_routing import simulate_ip_routing
# reuse the one-edge synthesizable model + helpers from test_whatif
from tests.model.test_whatif import _one_edge_model, _reg, MODE


def _ip_over_optical():
    m = _one_edge_model()
    m.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_0_1",),
                              mode_id=MODE, center_freq_hz=193.4e12))
    m.add_ip_link(IPLink(id="ip0", a_router="router_0", z_router="router_1",
                         lightpath_id="lp0"))
    m.add_service(Service(id="svc0", src_router="router_0", dst_router="router_1",
                          demand_gbps=300.0, working_path=("ip0",)))
    from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading
    recompute_qot_under_loading(model=m, store=QoTResultStore(), loading=loading_from_model(m))
    return m


def test_failure_zeroes_capacity_and_drops_service():
    base = _ip_over_optical()
    store = SnapshotStore(base)
    sid = store.create()
    store.branch(sid)
    branch = store.current()

    assert branch.ip_link_capacity_gbps("ip0") > 0    # healthy before
    inject_failure(branch, ("fiber_0_1_0",))
    assert branch.ip_link_capacity_gbps("ip0") == 0.0  # margin -inf -> capacity 0
    result = simulate_ip_routing(branch)
    assert "svc0" in {d.service_id for d in result.dropped}

    # ground truth untouched
    assert store.get(sid).ip_link_capacity_gbps("ip0") > 0


def test_nf_injection_can_drop_capacity_to_zero_when_margin_goes_negative():
    base = _ip_over_optical()
    store = SnapshotStore(base)
    sid = store.create()
    store.branch(sid)
    branch = store.current()

    # a large NF bump should push margin negative -> capacity 0 (margin gate)
    report = inject_degradation(branch, store=QoTResultStore(),
                                asset_id="amp_0_1_0", nf_delta=20.0)
    if branch.get_qot_state("lp0").margin_db < 0:
        assert branch.ip_link_capacity_gbps("ip0") == 0.0
        assert "lp0" in report.crossings
    # ground truth margin unchanged
    assert store.get(sid).get_qot_state("lp0").margin_db == base.get_qot_state("lp0").margin_db
```

> Adjust `Service.demand_gbps` / the NF delta so the assertions hold given the 400G mode's real GSNR on this topology. If margin does not go negative at +20 dB NF, raise the delta — do not weaken the assertion. The `dropped` accessor name (`result.dropped`, `d.service_id`) must match the phase-5 `IPRoutingResult` shape; check `model/ip_routing.py` and align.

- [ ] **Step 2: Run**

Run: `pytest tests/model/test_injection_layer_consistency.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/model/test_injection_layer_consistency.py
git commit -m "test(whatif): injection propagates to IP capacity/drops; ground truth isolated"
```

---

## Task 7: View serializers

**Files:**
- Modify: `src/multilayer_optical_mcp/model/views.py`
- Test: covered via Task 8 server tests

- [ ] **Step 1: Add serializers** (match the existing `*_dict` style in views.py):

```python
# append to src/multilayer_optical_mcp/model/views.py
def margin_sweep_dict(rows) -> dict:
    return {"fragile": [
        {"lightpath_id": r.lightpath_id, "margin_db": r.margin_db,
         "gsnr_db": r.gsnr_db, "mode_feasible": r.mode_feasible} for r in rows]}


def degradation_report_dict(report) -> dict:
    return {
        "asset_id": report.asset_id,
        "nf_delta": report.nf_delta,
        "loss_delta": report.loss_delta,
        "crossings": list(report.crossings),
        "rows": [
            {"lightpath_id": r.lightpath_id, "margin_before": r.margin_before,
             "margin_after": r.margin_after, "feasible_before": r.feasible_before,
             "feasible_after": r.feasible_after, "crossed": r.crossed,
             "within_threshold": r.within_threshold} for r in report.rows],
    }


def failure_report_dict(report) -> dict:
    return {"failed_assets": list(report.failed_assets),
            "downed_lightpaths": list(report.downed_lightpaths)}
```

- [ ] **Step 2: Commit**

```bash
git add src/multilayer_optical_mcp/model/views.py
git commit -m "feat(views): serializers for margin sweep, degradation, failure reports"
```

---

## Task 8: Expose the three tools

**Files:**
- Modify: `src/multilayer_optical_mcp/server.py`
- Test: `tests/test_server_phase6.py`

- [ ] **Step 1: Write the failing server tests** (follow `tests/test_server_phase5.py` for the FastMCP call pattern)

```python
# tests/test_server_phase6.py
import math
import pytest
from multilayer_optical_mcp.model.assets import Lightpath, IPLink, Router, Service
from multilayer_optical_mcp.model.qot import QoTState


def _seed_branch(app):
    """Create+branch a snapshot and return the branch model for direct seeding."""
    store = app._snapshots
    sid = store.create()
    store.branch(sid)
    return store.current(), store, sid


def test_whatif_sweep_tool_lists_fragile(server_app):
    app = server_app
    model, store, sid = _seed_branch(app)
    # seed a fragile + a safe lightpath (direct QoT, no GNPy needed for the sweep)
    # ... build oms/lightpaths as in test_whatif, set_qot_state margins 0.5 and 5.0
    # then call the tool:
    tool = app._tool_fns["whatif_margin_threshold_sweep"]
    out = tool(threshold_db=1.0)
    assert all(row["margin_db"] <= 1.0 for row in out["fragile"])


def test_inject_failure_tool_downs_and_isolates(server_app):
    app = server_app
    model, store, sid = _seed_branch(app)
    # build ip-over-optical as in test_injection_layer_consistency, recompute, then:
    tool = app._tool_fns["inject_failure"]
    out = tool(asset_ids=["fiber_0_1_0"])
    assert "lp0" in out["downed_lightpaths"]
    assert store.get(sid).ip_link_capacity_gbps("ip0") > 0  # ground truth intact
```

> The exact harness (`server_app` fixture, `app._tool_fns` accessor) must match `tests/test_server_phase5.py`. If phase 5 reaches tools differently (e.g. via the FastMCP client), copy that exact mechanism rather than inventing `_tool_fns`. Fill in the elided model-building with the same code used in `test_whatif.py`/`test_injection_layer_consistency.py` — do not leave it elided in the real test file.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_server_phase6.py -v`
Expected: FAIL (tools not registered).

- [ ] **Step 3: Register the tools** in `server.py` `build_app`. Add imports near the other model imports (line ~174):

```python
    from .model.whatif import (
        margin_threshold_sweep as _margin_sweep,
        inject_degradation as _inject_degradation,
        inject_failure as _inject_failure,
        loading_from_model,
    )
    from .model.views import (
        margin_sweep_dict, degradation_report_dict, failure_report_dict,
    )
```

and add the three tools alongside the others:

```python
    @app.tool()
    def whatif_margin_threshold_sweep(threshold_db: float) -> dict:
        """Physics-free screening: lightpaths whose current margin is within
        threshold_db of zero (margin_db <= threshold_db), sorted ascending.
        Models no degradation; makes no causal claim. Read-only."""
        rows = _margin_sweep(snapshots.current(), threshold_db)
        return margin_sweep_dict(rows)

    @app.tool()
    def inject_degradation(
        asset_id: str,
        nf_delta: float = 0.0,
        loss_delta: float = 0.0,
        threshold_db: float = 0.0,
    ) -> dict:
        """Branch what-if: add nf_delta dB NF to an amplifier and/or loss_delta dB
        loss to a fiber, recompute QoT under current loading, and return typed
        threshold crossings. Operate on a branch (snapshot_branch) — mutates state."""
        report = _inject_degradation(
            snapshots.current(), store=results, asset_id=asset_id,
            nf_delta=nf_delta, loss_delta=loss_delta, threshold_db=threshold_db)
        return degradation_report_dict(report)

    @app.tool()
    def inject_failure(asset_ids: list[str]) -> dict:
        """Branch what-if: mark assets failed; every lightpath crossing a failed
        asset goes down (capacity 0). Operate on a branch — mutates state."""
        report = _inject_failure(snapshots.current(), tuple(asset_ids))
        return failure_report_dict(report)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_server_phase6.py -v`
Expected: PASS.

- [ ] **Step 5: Full regression sweep**

Run: `pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/server.py tests/test_server_phase6.py
git commit -m "feat(server): expose whatif_margin_threshold_sweep/inject_degradation/inject_failure"
```

---

## Self-review checklist (run before handoff)

- [ ] **Spec coverage (CLAUDE.md What-if group):** `whatif_margin_threshold_sweep` physics-free screen (Task 3/8); `inject_degradation` perturb-recompute-report-crossings on a branch (Task 5/8); `inject_failure` mark-failed on a branch (Task 4/8). All present.
- [ ] **Margin is an output, never an input:** no code path sets `margin_db` from an argument; the sweep reads it, `inject_degradation` lets `recompute` produce it, `inject_failure` uses a −∞ sentinel (a "no signal" fact, not a dialled margin). Confirmed.
- [ ] **Branch isolation proven** (Task 6: ground truth untouched after inject on a branch) and snapshot clone/diff carry `_failed_assets` + the mutated amp/fiber registries (Task 2).
- [ ] **Margin-feasibility gate reused, not reimplemented:** capacity 0 comes from the existing `ip_link_capacity_gbps` margin check (Task 6), driven by injection setting margin negative / −∞.
- [ ] **Tool name** is `whatif_margin_threshold_sweep` (Tool-surface contract), not the stale build-order `whatif_margin_delta`.
- [ ] **Type consistency:** `DegradationReport`/`DegradationRow`/`FailureReport`/`MarginSweepRow`, `inject_degradation`/`inject_failure`/`margin_threshold_sweep`/`loading_from_model`, `apply_nf_delta`/`apply_loss_delta`/`mark_failed`/`is_failed`/`failed_assets` — names identical across model, whatif, views, server, tests.
- [ ] **Placeholder scan:** the two elided test bodies (Task 6 `dropped` accessor; Task 8 model-building + harness) carry explicit "fill this in / match phase-5" notes — resolve them against the real phase-5 code during execution, do not ship elisions.

---

## Out of scope (belongs to Step 7)

- `validate_plan` / `commit_plan` and the typed violation list (mode-infeasibility, transient overload, disjointness collapse). Injection here mutates a branch and reports; it does **not** gate a commit.
- Make-before-break overlap loadings (old ∪ new) and per-intermediate-state validation. `loading_from_model` deliberately builds only the committed channel set; constructed transient loadings are step-7's concern, evaluable via the same `recompute_qot_under_loading` (the 6a/step-2 arbitrary-loading contract).
