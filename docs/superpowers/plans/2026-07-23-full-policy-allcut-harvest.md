# FULL-policy all-CUT harvest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `FillPolicy.FULL` cheap at build scale by propagating the full-grid comb once per (OMS path, direction) and harvesting every probe slot's QoT from that single pass, then flip the default acceptance policy to FULL.

**Architecture:** Extract `compute_qot`'s propagation body into a shared `_propagate_loading` core so `harvest_qot` reuses identical physics but extracts all carriers instead of one. A new content-addressed `HarvestCache` (coarse, probe-free key) memoizes the per-slot vector. `make_adapter_evaluator` gains implicit full-grid detection: a full-grid `loading` routes to the harvest; any subset falls to today's `compute_qot`. Finally flip the ~10 `FillPolicy.ACTUAL` defaults to FULL and pin ACTUAL-specific tests.

**Tech Stack:** Python, GNPy 2.14.0 (`gn_model_analytic`, Raman off), pytest, NetworkX. Conda env `multilayer-optical-mcp`.

## Global Constraints

- Run all Python/pytest via `conda run -n multilayer-optical-mcp` (verbatim).
- Reference topologies use GNPy's advanced/explicit amplifier model; pin GNPy 2.14.0 for ground-truth.
- Read vs. mutate strictly separated; solver outcomes are typed, never exceptions.
- FULL is **acceptance-time only** — the operating recompute (`recompute_qot_under_loading`/`_per_path_loading`/settle) stays ACTUAL and is not touched by this plan.
- Content-addressed caches: no invalidation logic; a changed physical input must flip the key.
- No LLM in any test; deterministic and seedable.
- Do not commit the spec/plan `.md` files (per user instruction). Code commits are expected.

**Spec:** `docs/superpowers/specs/2026-07-23-full-policy-allcut-harvest-design.md`

---

### Task 1: Extract shared propagation core from `compute_qot`

Pure refactor — no behavior change. Moves the setup + SI-build + propagation loop into `_propagate_loading`, and the post-propagation penalty math into `_apply_penalties`. Existing `compute_qot` tests are the regression gate.

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/adapter.py` (extract from `compute_qot`, lines ~289–488)
- Test: `tests/gnpy_adapter/test_compute_qot.py` (existing, unchanged — must stay green)

**Interfaces:**
- Produces:
  - `_PropResult` — `NamedTuple(si, uids_list: list[str], elements: list, roadm_propagated: set[str], baud_rate: float, snapshots: list, final_gsnr_db: float, final_osnr_db: float)`
  - `_propagate_loading(model, oms_sequence, direction, loading_for_gnpy, mode, probe_idx, *, topo_path=None, eqpt_path=None) -> _PropResult`
  - `_apply_penalties(si, idx, uids_list, elements, roadm_propagated, baud_rate, gsnr_db, osnr_db) -> tuple[float, float]`

- [ ] **Step 1: Run the existing suite to capture the green baseline**

Run: `conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_compute_qot.py -v`
Expected: PASS (all 6 tests). This is the invariant the refactor must preserve.

- [ ] **Step 2: Add `_PropResult` and `_propagate_loading`**

Add near the other private helpers in `adapter.py`. Move the body of `compute_qot` **verbatim** from the setup comment (line 289 `# ---- setup`) through the end of the propagation loop / final-GSNR block (line 431 `final_osnr_db = ...`), parameterized by `probe_idx`:

```python
from typing import NamedTuple

class _PropResult(NamedTuple):
    si: object
    uids_list: list
    elements: list
    roadm_propagated: set
    baud_rate: float
    snapshots: list
    final_gsnr_db: float
    final_osnr_db: float

def _propagate_loading(model, oms_sequence, direction, loading_for_gnpy, mode,
                       probe_idx, *, topo_path=None, eqpt_path=None) -> "_PropResult":
    """Resolve the path, build the SI from *loading_for_gnpy*, propagate through
    every element, and return the final SI plus per-element snapshots taken at
    *probe_idx*. Shared by compute_qot (one probe) and harvest_qot (all slots)."""
    # >>> MOVE VERBATIM: adapter.py current lines 289-324 (setup + path resolve +
    #     drop-ROADM append). `network`, `uids`, `elements` are produced here.
    # >>> MOVE VERBATIM: adapter.py current lines 348-431, but:
    #     - the SI is built from `loading_for_gnpy` (already ensured >=2 & sorted);
    #     - the snapshot loop uses the passed `probe_idx` (was the local probe_idx);
    #     - `baud_rate = mode.symbol_rate_baud`.
    return _PropResult(si, uids_list, elements, _roadm_propagated,
                       mode.symbol_rate_baud, snapshots, final_gsnr_db, final_osnr_db)
```

- [ ] **Step 3: Add `_apply_penalties`**

Move the post-propagation penalty block (current lines 433–452) into a helper. It must read `si.tx_osnr[idx]` (per-carrier) and sum `add_drop_osnr` over `roadm_propagated`:

```python
def _apply_penalties(si, idx, uids_list, elements, roadm_propagated, baud_rate,
                     gsnr_db, osnr_db) -> tuple[float, float]:
    """Apply add_drop_osnr (per propagated ROADM) + tx_osnr (per carrier idx),
    normalised from 12.5 GHz to baud_rate via gnpy's snr_sum. Mirrors the block
    formerly inline in compute_qot."""
    from gnpy.core.elements import Roadm as _GnpyRoadm
    from gnpy.core.utils import snr_sum as _snr_sum, lin2db as _lin2db, db2lin as _db2lin
    penalties_noise_lin = 0.0
    for _uid, _el in zip(uids_list, elements):
        if isinstance(_el, _GnpyRoadm) and _uid in roadm_propagated:
            penalties_noise_lin += _db2lin(-_el.params.add_drop_osnr)
    penalties_noise_lin += _db2lin(-float(si.tx_osnr[idx]))
    if penalties_noise_lin > 0.0:
        combined = -_lin2db(penalties_noise_lin)
        gsnr_db = float(_snr_sum(gsnr_db, baud_rate, combined))
        osnr_db = float(_snr_sum(osnr_db, baud_rate, combined))
    return gsnr_db, osnr_db
```

- [ ] **Step 4: Rewrite `compute_qot` to call the extracted core**

Keep the cache lookup (271–287), probe selection (326–346), and `_ensure_min_two_channels`/`_find_probe_index` (348–351) in `compute_qot`. Replace the moved body with a call, then apply penalties, limiting-element, breakdown, and cache put exactly as before:

```python
    loading_for_gnpy = _ensure_min_two_channels(loading, probe.center_freq_hz)
    probe_idx = _find_probe_index(loading_for_gnpy, probe.center_freq_hz)
    pr = _propagate_loading(model, oms_sequence, direction, loading_for_gnpy, mode,
                            probe_idx, topo_path=topo_path, eqpt_path=eqpt_path)
    final_gsnr_db, final_osnr_db = _apply_penalties(
        pr.si, probe_idx, pr.uids_list, pr.elements, pr.roadm_propagated,
        pr.baud_rate, pr.final_gsnr_db, pr.final_osnr_db)
    margin_db = final_gsnr_db - mode.required_gsnr_db
    # limiting-element scan over pr.snapshots (move current lines 456-469 verbatim,
    # reading pr.snapshots), then build QoTBreakdown/QoTState/cache.put as today.
```

- [ ] **Step 5: Run the suite to verify no behavior change**

Run: `conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_compute_qot.py tests/gnpy_adapter -v`
Expected: PASS (all previously-green tests, including the ground-truth GSNR ~17.81 dB asserts). If any GSNR value shifts, the move was not verbatim — diff against the baseline.

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/adapter.py
git commit -m "refactor(adapter): extract _propagate_loading + _apply_penalties from compute_qot"
```

---

### Task 2: `harvest_qot` — one propagation, all slots

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/adapter.py`
- Test: `tests/gnpy_adapter/test_harvest_qot.py` (create)

**Interfaces:**
- Consumes: `_propagate_loading`, `_apply_penalties`, `_extract_gsnr_osnr`, `_path_physical_fingerprint` (Task 1 + existing).
- Produces:
  - `harvest_qot(model, oms_sequence, direction, mode_id, full_comb: LoadingState) -> dict[int, QoTState]` — key is grid slot index.
  - `harvest_cache_key(model, oms_sequence, direction, mode_id) -> tuple` = `(tuple(oms_sequence), direction.value, mode_id, _path_physical_fingerprint(model, oms_sequence, direction))`.

- [ ] **Step 1: Write the failing parity test**

```python
# tests/gnpy_adapter/test_harvest_qot.py
import math
import pytest
from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot, harvest_qot
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import Direction
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.spectrum import SpectrumGrid
from tests.gnpy_adapter.test_compute_qot import _toy_model

MODE = "400G@7.1dB"
GRID = SpectrumGrid.default()

def _full_comb(probe_slot):
    probe = Channel(GRID.freq(probe_slot), GRID.spacing_hz, None, MODE)
    others = tuple(Channel(GRID.freq(s), GRID.spacing_hz, None, MODE)
                   for s in range(GRID.num_slots) if s != probe_slot)
    return LoadingState((probe,) + others)

@pytest.mark.parametrize("direction", [Direction.FORWARD, Direction.BACKWARD])
def test_harvest_matches_per_slot_compute_qot(direction):
    n = _toy_model()
    oms = ("oms-AZ",) if direction == Direction.FORWARD else ("oms-AZ",)
    vec = harvest_qot(n, oms, direction, MODE, _full_comb(20))
    for slot in (10, 20, 30):
        state, _ = compute_qot(
            model=n, store=QoTResultStore(), oms_sequence=oms,
            direction=direction, mode_id=MODE, loading=_full_comb(slot),
            center_freq_hz=GRID.freq(slot))
        assert math.isclose(vec[slot].gsnr_db, state.gsnr_db, rel_tol=1e-9, abs_tol=1e-9)
        assert math.isclose(vec[slot].osnr_db, state.osnr_db, rel_tol=1e-9, abs_tol=1e-9)
```

- [ ] **Step 2: Run to verify it fails**

Run: `conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_harvest_qot.py -v`
Expected: FAIL — `ImportError: cannot import name 'harvest_qot'`.

- [ ] **Step 3: Implement `harvest_qot` and `harvest_cache_key`**

```python
def harvest_cache_key(model, oms_sequence, direction, mode_id):
    return (tuple(oms_sequence), direction.value, mode_id,
            _path_physical_fingerprint(model, oms_sequence, direction))

def harvest_qot(model, oms_sequence, direction, mode_id, full_comb):
    """Propagate the full-grid comb once; return {grid_slot: QoTState} for every
    carrier. No store writes, no per-slot breakdowns (acceptance discards them)."""
    from ..model.spectrum import SpectrumGrid
    grid = SpectrumGrid.default()
    mode = model.modes.get(mode_id)
    # Frequency-sort so SI index i aligns with gnpy's ascending SI arrays.
    sorted_channels = tuple(sorted(full_comb.channels, key=lambda c: c.center_freq_hz))
    loading_sorted = LoadingState(sorted_channels)
    pr = _propagate_loading(model, tuple(oms_sequence), direction, loading_sorted,
                            mode, probe_idx=0)  # snapshots at 0 are ignored
    out: dict[int, QoTState] = {}
    for i, ch in enumerate(sorted_channels):
        g, o = _extract_gsnr_osnr(pr.si, i)
        g, o = _apply_penalties(pr.si, i, pr.uids_list, pr.elements,
                                pr.roadm_propagated, pr.baud_rate, g, o)
        slot = grid.slot_of(ch.center_freq_hz)
        out[slot] = QoTState(gsnr_db=g, osnr_db=o,
                             margin_db=g - mode.required_gsnr_db,
                             limiting_element_id=None)
    return out
```

- [ ] **Step 4: Run to verify parity passes**

Run: `conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_harvest_qot.py -v`
Expected: PASS (both directions, slots 10/20/30 match within 1e-9).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/adapter.py tests/gnpy_adapter/test_harvest_qot.py
git commit -m "feat(adapter): harvest_qot extracts all-slot QoT from one full-comb propagation"
```

---

### Task 3: `HarvestCache`

**Files:**
- Modify: `src/multilayer_optical_mcp/model/qot_results.py`
- Test: `tests/model/test_harvest_cache.py` (create)

**Interfaces:**
- Produces: `HarvestCache` — `get(key) -> Optional[dict[int, QoTState]]`, `put(key, dict[int, QoTState]) -> None`, bounded LRU (`maxsize` default 4096). Mirror the existing `QoTCache` (`qot_results.py:43`).

- [ ] **Step 1: Write the failing test**

```python
# tests/model/test_harvest_cache.py
from multilayer_optical_mcp.model.qot_results import HarvestCache
from multilayer_optical_mcp.model.qot import QoTState

def _vec(g):
    return {0: QoTState(gsnr_db=g, osnr_db=30.0, margin_db=g - 7.1, limiting_element_id=None)}

def test_harvest_cache_get_put_roundtrip():
    c = HarvestCache()
    key = ("oms-AZ", "forward", "400G@7.1dB", ("fp",))
    assert c.get(key) is None
    c.put(key, _vec(17.8))
    assert c.get(key)[0].gsnr_db == 17.8

def test_harvest_cache_evicts_oldest():
    c = HarvestCache(maxsize=2)
    for i in range(3):
        c.put((i,), _vec(float(i)))
    assert c.get((0,)) is None      # evicted
    assert c.get((2,)) is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_harvest_cache.py -v`
Expected: FAIL — `ImportError: cannot import name 'HarvestCache'`.

- [ ] **Step 3: Implement `HarvestCache`**

```python
from collections import OrderedDict
from typing import Any, Dict, Optional

class HarvestCache:
    """Bounded LRU of full-comb harvest vectors, keyed by adapter.harvest_cache_key
    (path fingerprint + direction + mode — no probe frequency). Off-model, injected
    like QoTCache. Content-addressed: a changed physical input flips the key, so
    there is no invalidation logic."""
    def __init__(self, maxsize: int = 4096) -> None:
        self._store: "OrderedDict[Any, Dict[int, Any]]" = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: Any) -> Optional[Dict[int, Any]]:
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def put(self, key: Any, value: Dict[int, Any]) -> None:
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)
```

- [ ] **Step 4: Run to verify it passes**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_harvest_cache.py -v`
Expected: PASS (roundtrip + eviction).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/qot_results.py tests/model/test_harvest_cache.py
git commit -m "feat(cache): HarvestCache bounded-LRU for full-comb harvest vectors"
```

---

### Task 4: Full-grid detection in `make_adapter_evaluator`

**Files:**
- Modify: `src/multilayer_optical_mcp/model/allocation.py:54-68` (`make_adapter_evaluator`)
- Test: `tests/model/test_harvest_detection.py` (create)

**Interfaces:**
- Consumes: `harvest_qot`, `harvest_cache_key` (Task 2), `HarvestCache` (Task 3), `SpectrumGrid` (existing).
- Produces: `make_adapter_evaluator(model, store, *, topo_path=None, eqpt_path=None, cache=None, harvest_cache=None) -> QotEvaluator`. Full-grid `loading` → harvest branch; any subset → today's `compute_qot`.

- [ ] **Step 1: Write the failing detection + perf-smoke test**

```python
# tests/model/test_harvest_detection.py
from unittest.mock import patch
from multilayer_optical_mcp.model.allocation import make_adapter_evaluator
from multilayer_optical_mcp.model.qot_results import QoTResultStore, HarvestCache
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import Direction
from multilayer_optical_mcp.model.spectrum import SpectrumGrid
from tests.gnpy_adapter.test_compute_qot import _toy_model

MODE = "400G@7.1dB"
GRID = SpectrumGrid.default()

def _full_comb(probe_slot):
    probe = Channel(GRID.freq(probe_slot), GRID.spacing_hz, None, MODE)
    others = tuple(Channel(GRID.freq(s), GRID.spacing_hz, None, MODE)
                   for s in range(GRID.num_slots) if s != probe_slot)
    return LoadingState((probe,) + others)

def test_full_grid_harvests_once_across_probe_slots():
    n = _toy_model()
    hc = HarvestCache()
    ev = make_adapter_evaluator(n, QoTResultStore(), harvest_cache=hc)
    import multilayer_optical_mcp.model.allocation as alloc
    with patch.object(alloc, "harvest_qot", wraps=alloc.harvest_qot) as spy:
        for slot in (10, 20, 30):
            ev(oms_sequence=("oms-AZ",), direction=Direction.FORWARD,
               mode_id=MODE, loading=_full_comb(slot))
        assert spy.call_count == 1          # one propagation serves every probe slot

def test_subset_loading_does_not_harvest():
    n = _toy_model()
    hc = HarvestCache()
    ev = make_adapter_evaluator(n, QoTResultStore(), harvest_cache=hc)
    subset = LoadingState((Channel(GRID.freq(20), GRID.spacing_hz, None, MODE),))
    import multilayer_optical_mcp.model.allocation as alloc
    with patch.object(alloc, "harvest_qot", wraps=alloc.harvest_qot) as spy:
        ev(oms_sequence=("oms-AZ",), direction=Direction.FORWARD,
           mode_id=MODE, loading=subset)
        assert spy.call_count == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_harvest_detection.py -v`
Expected: FAIL — `make_adapter_evaluator` has no `harvest_cache` kwarg / `harvest_qot` not importable in `allocation`.

- [ ] **Step 3: Implement detection**

Rewrite `make_adapter_evaluator`:

```python
def make_adapter_evaluator(model, store, *, topo_path=None, eqpt_path=None,
                           cache=None, harvest_cache=None) -> QotEvaluator:
    from ..gnpy_adapter.adapter import compute_qot, harvest_qot, harvest_cache_key
    from .spectrum import SpectrumGrid
    grid = SpectrumGrid.default()

    def _eval(*, oms_sequence, direction, mode_id, loading):
        if harvest_cache is not None and topo_path is None and eqpt_path is None:
            try:
                slots = {grid.slot_of(c.center_freq_hz) for c in loading.channels}
            except ValueError:
                slots = None                      # off-grid channel -> normal path
            if slots is not None and len(slots) == grid.num_slots:
                key = harvest_cache_key(model, tuple(oms_sequence), direction, mode_id)
                vec = harvest_cache.get(key)
                if vec is None:
                    vec = harvest_qot(model, tuple(oms_sequence), direction,
                                      mode_id, loading)
                    harvest_cache.put(key, vec)
                # probe = the channel compute_qot would pick (first mode_id match =
                # channels[0] under FULL, which prepends the probe).
                probe = next(c for c in loading.channels if c.mode_id == mode_id)
                return vec[grid.slot_of(probe.center_freq_hz)]
        state, _ = compute_qot(
            model=model, store=store, oms_sequence=tuple(oms_sequence),
            direction=direction, mode_id=mode_id, loading=loading,
            topo_path=topo_path, eqpt_path=eqpt_path, cache=cache)
        return state
    return _eval
```

Note: `harvest_qot` must be imported at module scope in `allocation.py` too (the test patches `alloc.harvest_qot`) — add `from ..gnpy_adapter.adapter import harvest_qot` inside the lazy import block AND reference it via the module so the patch takes effect; simplest is to import it at the top of `_eval`'s module via the same lazy import and call `harvest_qot(...)` (patchable as `alloc.harvest_qot` only if bound at module level — so add a module-level `from ..gnpy_adapter.adapter import harvest_qot` guarded against import cycles, or have the test patch `adapter.harvest_qot` instead). Prefer: bind `harvest_qot` at module import of `allocation.py`; if that creates a cycle, patch `multilayer_optical_mcp.gnpy_adapter.adapter.harvest_qot` in the test instead and call it fully-qualified.

- [ ] **Step 4: Write the cache-fingerprint-completeness test**

```python
# append to tests/model/test_harvest_detection.py
import copy
from multilayer_optical_mcp.model.assets import Direction

def test_harvest_key_misses_when_fiber_loss_changes():
    n = _toy_model()
    hc = HarvestCache()
    ev = make_adapter_evaluator(n, QoTResultStore(), harvest_cache=hc)
    ev(oms_sequence=("oms-AZ",), direction=Direction.FORWARD,
       mode_id=MODE, loading=_full_comb(20))
    # mutate a GSNR-relevant physical input -> fingerprint must flip -> new propagation
    n2 = _toy_model()
    ft = n2.get_fiber_type("SSMF")
    n2.register_fiber_type(type(ft)(type_variety="SSMF", loss_coef_db_per_km=0.25))
    ev2 = make_adapter_evaluator(n2, QoTResultStore(), harvest_cache=hc)
    import multilayer_optical_mcp.model.allocation as alloc
    from unittest.mock import patch
    with patch.object(alloc, "harvest_qot", wraps=alloc.harvest_qot) as spy:
        ev2(oms_sequence=("oms-AZ",), direction=Direction.FORWARD,
            mode_id=MODE, loading=_full_comb(20))
        assert spy.call_count == 1          # different fiber loss -> cache miss
```

- [ ] **Step 5: Run all Task-4 tests**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_harvest_detection.py -v`
Expected: PASS (harvest-once, subset-skips, fingerprint-miss). If `register_fiber_type`'s exact constructor differs, adjust to the real `FiberType` signature in `assets.py`.

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/model/allocation.py tests/model/test_harvest_detection.py
git commit -m "feat(allocation): full-grid detection routes acceptance QoT through the harvest"
```

---

### Task 5: Flip the default to FULL + wire the shared cache + pin ACTUAL tests

**Files:**
- Modify: `src/multilayer_optical_mcp/model/allocation.py` (the ~9 `fill_policy: FillPolicy = FillPolicy.ACTUAL` defaults)
- Modify: `src/multilayer_optical_mcp/model/scenario.py:97` (default) and its `make_adapter_evaluator`/solver wiring
- Modify: `src/multilayer_optical_mcp/model/multilayer_graph.py:407-408` (`None -> FULL`)
- Modify: `src/multilayer_optical_mcp/server.py:358,374,425,507` (pass `harvest_cache=HarvestCache()`)
- Modify (pin ACTUAL): `tests/model/test_scenario.py`, `test_fill_policy.py`, `test_allocation.py`, `test_plan.py`, `test_rsa.py`, `test_views.py`, `tests/test_server_phase7.py`, `tests/gnpy_adapter/test_loading.py`

**Interfaces:**
- Consumes: `HarvestCache` (Task 3), the harvest-aware `make_adapter_evaluator` (Task 4).
- Produces: no new symbols — a default-value and wiring change. `FillPolicy.FULL` is the default for `solve_rsa`, `solve_allocation(_model)`, `place_demands`, `build_operating_network`.

- [ ] **Step 1: Flip the defaults**

Change every `fill_policy: FillPolicy = FillPolicy.ACTUAL` in `allocation.py` (lines 136, 179, 201, 223, 255, 331, 356, 379, 395) and `scenario.py:97` to `FillPolicy.FULL`. In `multilayer_graph.py:407-408` change the `None` fallback to `FillPolicy.FULL`.

- [ ] **Step 2: Wire a shared `HarvestCache` at the evaluator construction sites**

In `server.py`, at each `qot = make_adapter_evaluator(model, results)` (lines 358, 374, 425, 507), add a shared harvest cache alongside the existing QoT cache pattern:

```python
from .model.qot_results import HarvestCache
qot = make_adapter_evaluator(model, results, harvest_cache=HarvestCache())
```

If `scenario.build_operating_network` constructs its own evaluator internally, thread a `harvest_cache=HarvestCache()` there too so one cache is shared across the whole build.

- [ ] **Step 3: Run the full model suite to surface the flip's fallout**

Run: `conda run -n multilayer-optical-mcp pytest tests/model -q`
Expected: FAILs concentrated in mode-assertion tests (ACTUAL picked a higher mode than FULL). Capture the list — these are the pins for Step 4.

- [ ] **Step 4: Pin ACTUAL-specific tests explicitly**

For each failing test whose intent is to verify **ACTUAL** behavior, pass `fill_policy=FillPolicy.ACTUAL` explicitly to the solver/`build_operating_network` call in that test. Example (pattern to apply):

```python
from multilayer_optical_mcp.model.spectrum import FillPolicy
res = solve_rsa(_one_route(), qot, demand, fill_policy=FillPolicy.ACTUAL)  # pinned: ACTUAL-specific
```

Do NOT change the asserted values for pinned tests — they must keep asserting the ACTUAL result. Only tests that are genuinely policy-agnostic (assert "placed", not a specific mode) may ride the new FULL default unchanged.

- [ ] **Step 5: Run the full model suite green**

Run: `conda run -n multilayer-optical-mcp pytest tests/model tests/gnpy_adapter tests/test_server_phase7.py -q`
Expected: PASS. Any remaining red is either an un-pinned ACTUAL test (pin it) or a genuine FULL-default assertion to add in Task 6.

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/model/allocation.py src/multilayer_optical_mcp/model/scenario.py src/multilayer_optical_mcp/model/multilayer_graph.py src/multilayer_optical_mcp/server.py tests/
git commit -m "feat(policy): default acceptance FillPolicy to FULL; wire shared HarvestCache; pin ACTUAL tests"
```

---

### Task 6: New FULL-default assertions + german_17 E2E timing

**Files:**
- Modify: `tests/model/test_fill_policy.py` (add FULL-default assertions)
- Modify: `tests/model/test_scenario.py` (german_17 E2E)

**Interfaces:**
- Consumes: everything above. No new production symbols.

- [ ] **Step 1: Add a FULL-is-now-default assertion**

```python
# tests/model/test_fill_policy.py
def test_solve_rsa_defaults_to_full_downshift():
    """With the flipped default, solve_rsa (no fill_policy) now behaves as FULL:
    the dense comb forces the margin-stable lower mode, matching an explicit FULL."""
    qot = ChannelCountQot()
    demand = [{"id": "d1", "src": "A", "dst": "Z"}]
    default = solve_rsa(_one_route(), qot, demand)                       # no policy arg
    explicit_full = solve_rsa(_one_route(), qot, demand, fill_policy=FillPolicy.FULL)
    assert default.placements[0].working.mode_id == explicit_full.placements[0].working.mode_id
    assert default.placements[0].working.mode_id == "100G"
```

- [ ] **Step 2: Run it**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_fill_policy.py -v`
Expected: PASS.

- [ ] **Step 3: Confirm the german_17 FULL build completes and record the wall-time**

The E2E `test_german_17_end_to_end_real_adapter` now runs FULL by default with a shared `HarvestCache`. Add a timing capture (print, not a hard threshold — real-long-OMS per-call cost is unmeasured, spec §7):

```python
import time
t0 = time.perf_counter()
# ... existing build_operating_network(...) call ...
elapsed = time.perf_counter() - t0
print(f"[german_17 FULL build] {elapsed:.1f}s")
```

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_scenario.py::test_german_17_end_to_end_real_adapter -v -s`
Expected: PASS and completes (previously >39 min under FULL, uncached). Record the printed figure in the commit body.

- [ ] **Step 4: Profile one real long OMS (spec §7 open risk)**

Run the same `prof_full.py`-style timing but on a german_17 multi-span OMS (5+ fibers) instead of the 2-span toy, and record FULL/ACTUAL per-call ratio. If the ratio is still ~1.3×, the design assumption holds; if it climbs materially, note it for a follow-up (does not block this plan — the harvest's per-OMS collapse dominates regardless).

- [ ] **Step 5: Commit**

```bash
git add tests/model/test_fill_policy.py tests/model/test_scenario.py
git commit -m "test(policy): FULL-default assertions + german_17 FULL E2E timing"
```

---

## Self-Review

**Spec coverage:**
- §3.1 `_propagate_loading` → Task 1. §3.2 `harvest_qot` → Task 2. §3.3 `HarvestCache` → Task 3. §3.4 detection → Task 4. §4 flip + wiring + recompute-untouched → Task 5. §5 pin-ACTUAL policy → Task 5 Step 4. §6 tests: parity→T2, cache-completeness→T4, detection→T4, perf-smoke→T4, ACTUAL-pinned→T5, FULL-default→T6, E2E→T6. §7 real-long-OMS profile → T6 Step 4. All covered.

**Placeholder scan:** The two `# >>> MOVE VERBATIM` markers in Task 1 cite exact current line ranges (289–324, 348–431, 433–452, 456–469) to relocate unchanged — an explicit move instruction, not a vague "implement." All new code (harvest_qot, HarvestCache, detection, tests) is complete.

**Type consistency:** `harvest_qot` returns `dict[int, QoTState]`; `HarvestCache.get/put` use `Dict[int, QoTState]`; the evaluator does `vec[grid.slot_of(...)]` → `QoTState`. `harvest_cache_key` signature matches its call in the evaluator. `_PropResult` fields consumed in Task 2 (`pr.si`, `pr.uids_list`, `pr.roadm_propagated`, `pr.baud_rate`) match Task 1's definition. Consistent.

**Known data-dependent step:** Task 5 Step 3–4 (which tests fail, and their pins) is discovery-driven by design — the procedure and the exact pin mechanism (`fill_policy=FillPolicy.ACTUAL`) are specified; the per-test list is produced by the Step 3 run.
