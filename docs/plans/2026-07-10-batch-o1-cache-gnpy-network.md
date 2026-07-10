# Batch O1 — Cache the synthesized GNPy network/equipment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop rebuilding (and re-designing) the entire GNPy network on every `compute_qot`, and stop leaking a `mkdtemp()` per equipment build — without moving GSNR by a single micro-dB.

**Architecture:** `build_gnpy_network(model)` gains a module-level, fingerprint-keyed cache (a `weakref.WeakKeyDictionary` in `synthesize.py`, so no GNPy objects leak into the pure `NetworkModel` and the architecture boundary "the adapter is the only thing that talks to GNPy" holds). On a cache hit the cached `(equipment, network)` is returned after **restoring each EDFA's design-time `effective_gain`** — the *only* propagation-persisted mutation (`Edfa.interpol_params` does `self.effective_gain = min(self.effective_gain, p_max - pin_db)`, a monotonic ratchet), so a restore fully resets the operating point without re-running the 347 ms `design_network`. The temp-dir leak is fixed orthogonally: the config files are consumed at `load_equipment` time, so `build_gnpy_network` deletes the tmpdir in a `finally` immediately after equipment construction.

**Tech Stack:** Python 3, GNPy 2.14.0 (pinned), `weakref`, `pytest`, conda env `multilayer-optical-mcp`.

## Global Constraints

- **GNPy is pinned to `gnpy==2.14.0`.** The `effective_gain` ratchet
  (`elements.py Edfa.interpol_params`: `self.effective_gain = min(self.effective_gain, p_max - pin_db)`)
  and the fact that `design_network` sets a finite per-EDFA `effective_gain` are
  verified against exactly this version.
- **GSNR must not move.** Snapshot/restore of `effective_gain` reproduces a fresh
  build's GSNR *bit-for-bit* (verified: worst |Δ| = 0.0 dB across a 6-lightpath
  german_17 recompute). Every commit that touches this path re-runs
  `tests/gnpy_adapter/test_ground_truth_bridge.py` and it must stay green within
  `TOL_DB = 0.25`; the commit message states GSNR did **not** move and why.
- **The cache lives in the adapter, never in `NetworkModel`.** No GNPy object may be
  stored on the model. Use a module-level `weakref.WeakKeyDictionary` in
  `synthesize.py` keyed by the model instance.
- **The fingerprint covers physical state only** — fiber types, amplifiers, ROADMs,
  transceivers, fibers, OMS. It must **exclude** `_qot_state`, lightpaths, IP layer,
  services, risk groups, and failed assets, so that `set_qot_state` calls made *inside*
  `recompute_qot_under_loading`'s own loop do not invalidate the cache mid-loop.
- **Single-threaded assumption.** The server is single-threaded; the cache is not
  guarded for concurrent access. Document this in the module docstring.
- All commands run via `conda run -n multilayer-optical-mcp` with `PYTHONPATH=src`
  (there is no editable install; `tests/conftest.py` puts `src` on the path for pytest).

---

## File Structure

- `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py` — **modified**. Add
  `_physical_fingerprint`, the `_NETWORK_CACHE` WeakKeyDictionary, `_CacheEntry`,
  `_snapshot_design_gains`, `_restore_design_gains`; rewrite `build_gnpy_network`
  to (a) delete its tmpdir immediately and (b) consult/populate the cache.
- `tests/gnpy_adapter/test_o1_network_cache.py` — **new**. All Batch O1 unit and
  integration tests (leak, fingerprint, reuse identity, invalidation, no-drift,
  single-synthesis).
- `tests/gnpy_adapter/test_ground_truth_bridge.py` — **unchanged assertions**; run as
  the physics gate.

---

## Task 1: Fix the temp-dir leak (immediate cleanup)

Orthogonal to caching and independently shippable: today `model_to_gnpy_equipment`
does `Path(tempfile.mkdtemp())` per call and never removes it (`synthesize.py:45-46`),
and `_equipment_from_dict` can `mkdtemp()` a second one (`:230`). The `adv_nf_*.json`
and `eqpt.json` files are read once, at `load_equipment` time, and are never reopened
during design or propagation — so the directory can be deleted the instant equipment
construction returns.

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py` — `build_gnpy_network`
  (currently `:169-180`).
- Test: `tests/gnpy_adapter/test_o1_network_cache.py`

**Interfaces:**
- Consumes: `model_to_gnpy_equipment(model, _tmpdir)`, `_equipment_from_dict(dict)`,
  `model_to_gnpy_topology(model)`, `gnpy_design_network(network, equipment)` — all
  existing, unchanged signatures.
- Produces: `build_gnpy_network(model) -> (equipment, network)` — same return contract,
  but leaves **zero** temp directories behind.

- [ ] **Step 1: Write the failing test**

```python
# tests/gnpy_adapter/test_o1_network_cache.py
import json
import tempfile
from pathlib import Path

import pytest

from multilayer_optical_mcp.gnpy_adapter import synthesize as S
from multilayer_optical_mcp.model.assets import (
    Amplifier, Fiber, FiberType, OMS, ROADM, Transceiver, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel

MODE = TransceiverMode(id="400G@7.1dB", bitrate_gbps=400.0, required_gsnr_db=7.1,
                       symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)


def _toy_model() -> NetworkModel:
    """Two-span toy identical to test_ground_truth_bridge._toy_model_synthesized."""
    n = NetworkModel(modes=ModeRegistry([MODE]))
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
    n.add_oms(OMS(id="oms_syn", src_node_id="A", dst_node_id="Z", elements=(
        "roadm_A", "amp_booster", "fiber_0", "amp_ila",
        "fiber_1", "amp_preamp")))
    return n


def test_build_leaves_no_temp_dirs(monkeypatch):
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*a, **k):
        p = real_mkdtemp(*a, **k)
        created.append(Path(p))
        return p

    monkeypatch.setattr(tempfile, "mkdtemp", spy_mkdtemp)
    model = _toy_model()
    for _ in range(3):
        S.build_gnpy_network(model)

    assert created, "expected build_gnpy_network to create at least one temp dir"
    still_there = [p for p in created if p.exists()]
    assert not still_there, f"orphaned temp dirs left behind: {still_there}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_o1_network_cache.py::test_build_leaves_no_temp_dirs -v`
Expected: FAIL — orphaned temp dirs remain (current code never removes the `mkdtemp()`).

- [ ] **Step 3: Rewrite `build_gnpy_network` to own and delete its tmpdir**

Replace the body of `build_gnpy_network` (`synthesize.py:169-180`) with:

```python
def build_gnpy_network(model: NetworkModel):
    """Return (equipment, network) built from the model, ready to propagate.

    Reuses gnpy's network_from_json + design_network so synthesized results
    match a hand-written topology of the same shape.

    The equipment config files (adv_nf_*.json, eqpt.json) are consumed at
    load_equipment time and never reopened afterwards, so the temp directory is
    deleted the instant equipment construction returns — no orphaned dirs.
    """
    import shutil
    from gnpy.tools.json_io import network_from_json

    tmpdir = Path(tempfile.mkdtemp())
    try:
        equipment = _equipment_from_dict(model_to_gnpy_equipment(model, _tmpdir=tmpdir))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    network = network_from_json(model_to_gnpy_topology(model), equipment)
    gnpy_design_network(network, equipment)
    return equipment, network
```

- [ ] **Step 4: Run the leak test and the physics gate**

Run: `PYTHONPATH=src conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_o1_network_cache.py::test_build_leaves_no_temp_dirs tests/gnpy_adapter/test_ground_truth_bridge.py -v`
Expected: PASS (both) — leak fixed, GSNR unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/synthesize.py tests/gnpy_adapter/test_o1_network_cache.py
git commit -m "perf(synthesize): scope equipment tmpdir to build_gnpy_network, fix mkdtemp leak (O1)

Config files are consumed at load_equipment time; delete the tmpdir in a
finally immediately after. GSNR unchanged (temp-dir scoping only)."
```

---

## Task 2: `_physical_fingerprint(model)`

A pure function over the model's **physical** state — everything
`model_to_gnpy_equipment` / `model_to_gnpy_topology` read, and nothing else. This is
the cache key. It is intentionally *self-invalidating*: any physical mutation
(`apply_nf_delta`, `apply_loss_delta`, `add_fiber`, `register_fiber_type`, …) changes
the fingerprint, so no explicit per-mutator invalidation hook is needed, and
`set_qot_state` (called inside the recompute loop) cannot invalidate it.

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py`
- Test: `tests/gnpy_adapter/test_o1_network_cache.py`

**Interfaces:**
- Consumes: `model.list_fiber_types()`, `model._amplifiers`, `model._roadms`,
  `model._transceivers`, `model._fibers`, `model.list_oms()`.
- Produces: `_physical_fingerprint(model) -> tuple` — hashable, order-independent
  (each collection sorted), suitable as a cache key and comparable with `==`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/gnpy_adapter/test_o1_network_cache.py

def test_fingerprint_stable_across_qot_mutation():
    from multilayer_optical_mcp.model.qot import QoTState
    from multilayer_optical_mcp.model.assets import Lightpath

    model = _toy_model()
    model.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_syn",),
                                  mode_id=MODE.id, center_freq_hz=193.4e12))
    fp_before = S._physical_fingerprint(model)
    # A QoT write is NOT physical state — must not change the fingerprint.
    model.set_qot_state("lp0", QoTState(gsnr_db=18.0, osnr_db=25.0,
                                        margin_db=10.9, limiting_element_id=None))
    assert S._physical_fingerprint(model) == fp_before


def test_fingerprint_changes_on_nf_delta():
    model = _toy_model()
    fp_before = S._physical_fingerprint(model)
    model.apply_nf_delta("amp_ila", 2.0)
    assert S._physical_fingerprint(model) != fp_before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_o1_network_cache.py -k fingerprint -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_physical_fingerprint'`.

- [ ] **Step 3: Implement `_physical_fingerprint`**

Add near the top of `synthesize.py` (after the module constants):

```python
def _physical_fingerprint(model: NetworkModel) -> tuple:
    """Order-independent hashable key over exactly the model state that feeds
    model_to_gnpy_equipment / model_to_gnpy_topology.

    EXCLUDES _qot_state, lightpaths, the IP layer, services, risk groups, and
    failed assets — none of those touch the synthesized GNPy network, and
    set_qot_state is called inside recompute_qot_under_loading's own loop, so
    including it would invalidate the cache mid-recompute.
    """
    fiber_types = tuple(sorted(
        (ft.type_variety, ft.loss_coef_db_per_km, ft.dispersion,
         ft.effective_area, ft.pmd_coef)
        for ft in model.list_fiber_types()))
    amps = tuple(sorted(
        (a.id, a.type_variety, a.gain_db, a.nf_db, a.tilt_db)
        for a in model._amplifiers.values()))
    roadms = tuple(sorted(
        (r.id, r.target_pch_out_db) for r in model._roadms.values()))
    transceivers = tuple(sorted(
        (t.id, t.site) for t in model._transceivers.values()))
    fibers = tuple(sorted(
        (f.id, f.type_variety, f.length_km, f.extra_loss_db, f.a_end, f.z_end)
        for f in model._fibers.values()))
    oms = tuple(sorted(
        (o.id, o.src_node_id, o.dst_node_id, tuple(o.elements))
        for o in model.list_oms()))
    return (fiber_types, amps, roadms, transceivers, fibers, oms)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_o1_network_cache.py -k fingerprint -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/synthesize.py tests/gnpy_adapter/test_o1_network_cache.py
git commit -m "perf(synthesize): add _physical_fingerprint (physical-state-only cache key) (O1)"
```

---

## Task 3: Cache `(equipment, network)` with design-gain snapshot/restore

The heart of the batch. On a cache hit, return the cached objects after restoring
each EDFA's design-time `effective_gain` (undoing the propagation ratchet). On a miss,
build fresh, snapshot the gains, store the entry keyed by the model in a
`WeakKeyDictionary` (auto-evicts when the model is garbage-collected).

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py`
- Test: `tests/gnpy_adapter/test_o1_network_cache.py`

**Interfaces:**
- Consumes: `_physical_fingerprint(model)` (Task 2), the Task 1 `build_gnpy_network`
  body (fresh-build path), `gnpy.core.elements.Edfa`.
- Produces:
  - `_snapshot_design_gains(network) -> dict[str, float]` — `{edfa.uid: effective_gain}`.
  - `_restore_design_gains(network, gains) -> None`.
  - `_NETWORK_CACHE: WeakKeyDictionary` and `_CacheEntry` (namedtuple:
    `fingerprint, equipment, network, design_gains`).
  - `build_gnpy_network(model)` returns the **same** `(equipment, network)` object
    identities on repeated calls with an unchanged model; a physical mutation yields
    fresh objects.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/gnpy_adapter/test_o1_network_cache.py

def test_repeated_build_returns_same_objects():
    model = _toy_model()
    eq1, net1 = S.build_gnpy_network(model)
    eq2, net2 = S.build_gnpy_network(model)
    assert eq1 is eq2
    assert net1 is net2


def test_physical_mutation_rebuilds():
    model = _toy_model()
    _eq1, net1 = S.build_gnpy_network(model)
    model.apply_nf_delta("amp_ila", 2.0)
    _eq2, net2 = S.build_gnpy_network(model)
    assert net2 is not net1


def test_cached_reuse_matches_fresh_gsnr_no_drift():
    """A heavy loading then a light probe on the SAME (cached) network must give
    the identical probe GSNR a fresh build would — proving effective_gain is reset."""
    from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
    from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
    from multilayer_optical_mcp.model.assets import Direction
    from multilayer_optical_mcp.model.qot_results import QoTResultStore

    probe = Channel(193.4e12, 100e9, None, MODE.id)
    heavy = LoadingState(channels=tuple(
        Channel(193.0e12 + i * 100e9, 100e9, None, MODE.id) for i in range(6)))
    light = LoadingState(channels=(probe,))

    # Fresh model, single light computation = ground truth.
    fresh = _toy_model()
    store_f = QoTResultStore()
    g_fresh, _ = compute_qot(model=fresh, store=store_f, oms_sequence=("oms_syn",),
                             direction=Direction.FORWARD, mode_id=MODE.id,
                             loading=light, center_freq_hz=193.4e12)

    # Same model reused: heavy first (ratchets gains), then the light probe.
    reused = _toy_model()
    store_r = QoTResultStore()
    compute_qot(model=reused, store=store_r, oms_sequence=("oms_syn",),
                direction=Direction.FORWARD, mode_id=MODE.id, loading=heavy,
                center_freq_hz=193.4e12)
    g_reuse, _ = compute_qot(model=reused, store=store_r, oms_sequence=("oms_syn",),
                             direction=Direction.FORWARD, mode_id=MODE.id,
                             loading=light, center_freq_hz=193.4e12)

    assert g_reuse.gsnr_db == g_fresh.gsnr_db, (
        f"cached reuse drifted: reuse={g_reuse.gsnr_db} fresh={g_fresh.gsnr_db}")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_o1_network_cache.py -k "repeated or rebuilds or no_drift" -v`
Expected: FAIL — `test_repeated_build_returns_same_objects` fails (`net1 is not net2`,
no cache yet); the drift test may pass by luck (fresh build every call) but must keep
passing after the cache lands.

- [ ] **Step 3: Add the cache machinery and wire it into `build_gnpy_network`**

Add near the top of `synthesize.py` (after imports):

```python
from collections import namedtuple
from weakref import WeakKeyDictionary

# Module-level, single-threaded cache of the synthesized GNPy network per model.
# Keyed by the NetworkModel instance (weak, so a dropped branch is auto-evicted);
# the entry is reused only while the model's physical fingerprint is unchanged.
# No GNPy object is ever stored on the NetworkModel itself — the adapter remains
# the only component that holds GNPy state.
_CacheEntry = namedtuple("_CacheEntry", "fingerprint equipment network design_gains")
_NETWORK_CACHE: "WeakKeyDictionary[NetworkModel, _CacheEntry]" = WeakKeyDictionary()


def _snapshot_design_gains(network) -> Dict[str, float]:
    """Capture each EDFA's design-time effective_gain (set by design_network)."""
    from gnpy.core.elements import Edfa
    return {n.uid: n.effective_gain
            for n in network.nodes if isinstance(n, Edfa)}


def _restore_design_gains(network, gains: Dict[str, float]) -> None:
    """Reset each EDFA's effective_gain to its design value.

    Propagation ratchets effective_gain DOWN in place
    (``self.effective_gain = min(self.effective_gain, p_max - pin_db)`` in
    ``Edfa.interpol_params``); this restore undoes the ratchet so a reused
    network propagates as if freshly designed. Verified GSNR-identical (|Δ|=0.0 dB)
    on GNPy 2.14.0.
    """
    from gnpy.core.elements import Edfa
    for n in network.nodes:
        if isinstance(n, Edfa) and n.uid in gains:
            n.effective_gain = gains[n.uid]
```

Then rewrite `build_gnpy_network` (from Task 1) to consult the cache:

```python
def build_gnpy_network(model: NetworkModel):
    """Return (equipment, network) built from the model, ready to propagate.

    Cached per model instance and keyed by the model's physical fingerprint: a
    repeated call with an unchanged model returns the same objects after resetting
    every EDFA to its design-time operating point (undoing the propagation
    effective_gain ratchet), so a bulk recompute over K lightpaths synthesizes the
    network exactly once. Any physical mutation changes the fingerprint and triggers
    a fresh build. Single-threaded; not guarded for concurrent access.

    The equipment config files (adv_nf_*.json, eqpt.json) are consumed at
    load_equipment time and never reopened, so the temp directory is deleted
    immediately after equipment construction.
    """
    import shutil
    from gnpy.tools.json_io import network_from_json

    fingerprint = _physical_fingerprint(model)
    entry = _NETWORK_CACHE.get(model)
    if entry is not None and entry.fingerprint == fingerprint:
        _restore_design_gains(entry.network, entry.design_gains)
        return entry.equipment, entry.network

    tmpdir = Path(tempfile.mkdtemp())
    try:
        equipment = _equipment_from_dict(model_to_gnpy_equipment(model, _tmpdir=tmpdir))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    network = network_from_json(model_to_gnpy_topology(model), equipment)
    gnpy_design_network(network, equipment)

    _NETWORK_CACHE[model] = _CacheEntry(
        fingerprint=fingerprint, equipment=equipment, network=network,
        design_gains=_snapshot_design_gains(network))
    return equipment, network
```

- [ ] **Step 4: Run the Task 3 tests plus the physics gate**

Run: `PYTHONPATH=src conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_o1_network_cache.py tests/gnpy_adapter/test_ground_truth_bridge.py -v`
Expected: PASS (all) — same-object reuse, rebuild on mutation, no GSNR drift, ground
truth unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/synthesize.py tests/gnpy_adapter/test_o1_network_cache.py
git commit -m "perf(synthesize): cache synthesized GNPy network per model fingerprint (O1)

WeakKeyDictionary keyed by model + physical fingerprint; cache hit restores each
EDFA's design-time effective_gain (the only propagation-persisted mutation) so
reuse is GSNR-identical to a fresh build (|delta|=0.0 dB, GNPy 2.14.0). No GNPy
object stored on NetworkModel. GSNR unchanged."
```

---

## Task 4: Integration — one synthesis per bulk recompute

The batch's headline test (roadmap: "bulk recompute over K lightpaths performs one
synthesis; no orphaned temp dirs"). Spy on the *expensive inner step*
(`model_to_gnpy_topology`, called once per fresh build — NOT the cached
`build_gnpy_network` entry point) and assert it runs exactly once across a
`recompute_qot_under_loading` over several lightpaths, with zero temp dirs left.

**Files:**
- Test: `tests/gnpy_adapter/test_o1_network_cache.py`

**Interfaces:**
- Consumes: `recompute_qot_under_loading` (adapter), `build_gnpy_network` (Task 3),
  `synthesize.model_to_gnpy_topology`.
- Produces: no production code — verification only.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/gnpy_adapter/test_o1_network_cache.py

def _toy_model_with_lightpaths(n_lp: int) -> NetworkModel:
    from multilayer_optical_mcp.model.assets import Lightpath
    model = _toy_model()
    for i in range(n_lp):
        model.add_lightpath(Lightpath(
            id=f"lp{i}", oms_sequence=("oms_syn",), mode_id=MODE.id,
            center_freq_hz=193.0e12 + i * 100e9))
    return model


def test_bulk_recompute_synthesizes_once(monkeypatch):
    import tempfile
    from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading
    from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
    from multilayer_optical_mcp.model.qot_results import QoTResultStore

    calls = {"topology": 0}
    real_topology = S.model_to_gnpy_topology

    def spy_topology(model):
        calls["topology"] += 1
        return real_topology(model)

    monkeypatch.setattr(S, "model_to_gnpy_topology", spy_topology)

    created = []
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp",
                        lambda *a, **k: created.append(Path(real_mkdtemp(*a, **k)))
                        or str(created[-1]))

    model = _toy_model_with_lightpaths(4)
    store = QoTResultStore()
    loading = LoadingState(channels=tuple(
        Channel(193.0e12 + i * 100e9, 100e9, None, MODE.id) for i in range(4)))

    recompute_qot_under_loading(model=model, store=store, loading=loading)

    assert calls["topology"] == 1, (
        f"expected exactly one synthesis across the bulk recompute, "
        f"got {calls['topology']}")
    assert not [p for p in created if p.exists()], "orphaned temp dirs after recompute"
```

- [ ] **Step 2: Run the test**

Run: `PYTHONPATH=src conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_o1_network_cache.py::test_bulk_recompute_synthesizes_once -v`
Expected: PASS (Task 3 already delivered the cache). If it reports `> 1`, the cache is
being invalidated mid-loop — check that `_physical_fingerprint` excludes `_qot_state`
(`set_qot_state` runs inside the recompute loop).

- [ ] **Step 3: Run the full suite (regression + physics gate)**

Run: `PYTHONPATH=src conda run -n multilayer-optical-mcp pytest -q`
Expected: PASS — full suite green, including `test_ground_truth_bridge.py`,
`tests/model/test_layer_consistency.py`, `test_injection_layer_consistency.py`, and the
existing `test_synthesize.py` / `test_c8_diagnostics.py` that call `build_gnpy_network`
directly.

- [ ] **Step 4: Commit**

```bash
git add tests/gnpy_adapter/test_o1_network_cache.py
git commit -m "test(synthesize): bulk recompute synthesizes once, no temp-dir leak (O1)"
```

---

## Verification (whole batch)

- `PYTHONPATH=src conda run -n multilayer-optical-mcp pytest -q` green before each commit.
- Physics gate: `tests/gnpy_adapter/test_ground_truth_bridge.py` green within
  `TOL_DB`. GSNR **did not move** — the cache reuses a design-identical network with
  `effective_gain` restored to its design value (proven |Δ| = 0.0 dB on a
  6-lightpath german_17 recompute); state this in each physics-touching commit message.
- Layer-consistency oracle unaffected (no model semantics changed).
- Roadmap O1 acceptance: bulk recompute over K lightpaths performs one synthesis
  (Task 4) and leaves no orphaned `adv_nf_*`/temp dirs (Tasks 1 & 4).

## Explicitly NOT in this batch (per master plan)

- The findings-doc C2/C3 **single-channel fast path / NLI overload** — wrong
  bottleneck, highest-risk surface. Do NOT build it. Revisit only if post-O1 profiling
  shows NLI itself dominates. Profiling here already shows the win is in avoiding
  redundant `design_network` (347 ms) + parse/equipment-load (238 ms), which the cache
  captures without touching the NLI path.

## Deviation from the master plan's literal wording

The master plan says "invalidate on mutation (composes with C3's `_qot_state`
invalidation hooks)". This plan uses a **pull-based physical fingerprint** instead of a
push-based per-mutator invalidation hook. Rationale: the fingerprint is self-invalidating
(any physical mutation changes it; no mutator can forget to invalidate) and it cannot be
tripped by the `set_qot_state` calls made *inside* `recompute_qot_under_loading`'s own
loop — which a coarse "bump on any mutation" counter would, defeating the entire batch.
It composes with C3 by construction: physical mutations change the key; QoT-state
mutations do not.
```
