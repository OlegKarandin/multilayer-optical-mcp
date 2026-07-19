# multilayer-optical-mcp — Phase 5: IP-over-optical layer (couplings + `simulate_ip_routing` + IP tools)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement each function test-first. Read tools never mutate; `simulate_ip_routing` is a **pure read** that computes nothing it doesn't account for. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land Build-order **Step 5 (IP-over-optical layer)** — complete the three cross-layer couplings as a consistent data model, add the read-only `simulate_ip_routing`, prove that an optical change (mode downshift / margin-negative) propagates up into reduced IP capacity and congestion **on a branch**, and only then expose the IP tools (`get_ip_topology`, `get_grooming_map`, `get_affected_services`, `simulate_ip_routing`, `reroute_service`).

**Architecture:** Pure deterministic functions over `NetworkModel`. `simulate_ip_routing` **reads** each service's pinned `working_path` and **accounts** demand onto IP links; it never routes. Shortest-path computes paths only at routing / `reroute_service` time. Capacity is the already-built margin-gated `ip_link_capacity_gbps` (a downshift halves it; a negative margin zeroes it). The grooming map and traffic matrix are **derived** from services — one source of truth, no second demand store.

**Tech Stack:** Python 3.11+, FastMCP, NetworkX ≥3.2, pytest. No new deps. No GNPy required for the layer-consistency proof (margin is set directly, exactly as `tests/model/test_capacity_coupling.py` already does).

---

## Context (what already exists — do not rebuild)

Steps 1–4 are committed. Specifically relevant to Step 5:

- **Coupling #1 (IP link ⇄ lightpath) — partial.** `IPLink.lightpath_id` exists and is validated in `NetworkModel.add_ip_link`. The **reverse** lookup (lightpath → IP links) does **not** exist yet — this plan adds it (Task 1).
- **Coupling #2 (capacity = f(mode), margin-gated) — DONE.** `NetworkModel.ip_link_capacity_gbps(link_id)` returns `modes.get(mode_id).bitrate_gbps` when `margin_db ≥ 0`, `0.0` when `margin_db < 0`, and raises `LookupError` when no QoT state is recorded. Tested in `tests/model/test_capacity_coupling.py`. **Reuse it; do not reimplement capacity.**
- **Coupling #3 (grooming) — partial.** `model/views.py:services_dict` already derives a `lightpath → [service_ids]` map inline, and `traffic_matrix_dict` already aggregates `Service.demand_gbps` by `(src_router, dst_router)`. This plan promotes grooming to a first-class model function with **both** directions (Task 2) and a dedicated tool (Task 8).
- **State engine.** `SnapshotStore.branch(parent_id)` is copy-on-write and clones `_services`, `_lightpaths`, `_ip_links`, `_qot_state` (see `model/snapshots.py`). The layer-consistency proof (Task 4) branches, mutates the branch, and asserts ground truth is untouched.
- **Asset-set helpers.** `model/exposure.py:_path_asset_set(model, ip_link_ids)` expands an IP-link sequence to `{ip_link_ids} ∪ {lightpath_ids} ∪ {oms_ids} ∪ {fiber/amp/roadm uids}`. `get_affected_services` (Task 6) reuses it so a single membership test is layer-agnostic.
- **Server pattern.** `server.py:build_app()` holds a `SnapshotStore`; every tool reads `snapshots.current()`. Serializers live in `model/views.py` and are imported inside `build_app`. Follow this pattern exactly.

---

## Decisions settled

**With the user (this brainstorming session):**

1. **`simulate_ip_routing` reads pinned per-service `working_path`; it does not route.** Shortest-path (IGP-style) is used by the *solvers* and by `reroute_service` to **compute** a path, which is then pinned. This keeps `simulate` a pure read, keeps the grooming map a single source of truth, and makes restoration an explicit, auditable `reroute_service` action rather than emergent self-healing. (Rejected: IGP recomputation inside `simulate` — it would smuggle a routing *policy* into a read tool and make "which demands ride lightpath_X" a fractional, unstable answer.)
2. **`Service` is the single demand unit; the traffic matrix is a derived aggregate.** A `Service` with an empty `working_path` is an unrouted demand. No separate demand store. (Already reflected by `traffic_matrix_dict`.)

**Settled defaults in this plan (call out at spec review if you disagree):**

3. **Only `working_path` carries load in baseline `simulate`.** `protection_path` is standby and contributes **zero** load until a `reroute_service` (or a future protection switch) pins traffic onto it. Loading both would double-count every protected service.
4. **Drop model is link-local and deterministic — not max-flow / fair-share TE.** Per IP link: `offered_gbps` = Σ demand of services whose `working_path` includes it; `capacity_gbps` = `ip_link_capacity_gbps`. A link is **down** when `capacity == 0` (margin-negative or torn-down lightpath) and **congested** when `utilization > 1`. The result reports, without conflating them: per-link utilizations, the `down`/`congested` link id sets, the services that cross a **down** link (full demand lost, reason `link_down`), and the total link-local **overflow** (`Σ max(0, offered − capacity)` over congested-but-not-down links). No traffic is re-routed to absorb overflow — that is the agent's explicit `reroute_service` decision. This is a deliberately simple, honest first model; it makes **no** claim of optimal flow placement.
5. **`reroute_service` is a branch-level mutation here; the validate/commit gate is Step 7.** It re-pins `working_path` to a caller-supplied `ip_path` and validates that path is contiguous `src_router → dst_router` (links usable in either orientation) and that every link exists. Invalid input raises `ValueError` at the model layer (typed structured error surfacing is Step 7). Path *computation* stays the caller's job (`compute_paths`), consistent with decision 1.

---

## File structure

- **Create `src/multilayer_optical_mcp/model/ip_routing.py`** — the IP-layer logic that changes together: offered-load accounting, the `simulate_ip_routing` result types + function, the grooming map, `affected_services`, and the path-contiguity helper. One responsibility: *read the consistent multilayer model and report IP-layer consequences.*
- **Modify `src/multilayer_optical_mcp/model/network.py`** — add `ip_links_for_lightpath` (coupling #1 reverse) and `set_service_working_path` (used by `reroute_service`).
- **Modify `src/multilayer_optical_mcp/model/views.py`** — add `ip_topology_dict`, `grooming_map_dict`, `ip_routing_result_dict`, `affected_services_dict`.
- **Modify `src/multilayer_optical_mcp/server.py`** — expose `get_ip_topology`, `get_grooming_map`, `get_affected_services`, `simulate_ip_routing`, `reroute_service`.
- **Create `tests/model/test_ip_routing.py`** — offered-load, simulate, grooming, affected-services, contiguity.
- **Create `tests/model/test_layer_consistency.py`** — the headline branch proof (downshift → congestion; margin-negative → capacity 0 → dropped) with ground truth untouched.
- **Create `tests/test_server_phase5.py`** — the five tools end-to-end through FastMCP.

---

# Part A — model + `simulate_ip_routing` (no new tools)

## Task 1: Coupling #1 reverse lookup — `ip_links_for_lightpath`

**Files:**
- Modify: `src/multilayer_optical_mcp/model/network.py`
- Test: `tests/model/test_ip_routing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/model/test_ip_routing.py
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, Lightpath, Router, IPLink, Service,
    TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState


def _two_link_model():
    """A→B→C: two IP links, each bound to its own single-OMS lightpath."""
    reg = ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
        TransceiverMode(id="200G-16QAM", bitrate_gbps=200.0, required_gsnr_db=18.5,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for nid in ("a1", "a2", "a3"):
        n.add_amplifier(Amplifier(id=nid, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0,
                      type_variety="SSMF"))
    n.add_fiber(Fiber(id="fBC", a_end="a2", z_end="a3", length_km=80.0,
                      type_variety="SSMF"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B",
                  elements=("a1", "fAB", "a2")))
    n.add_oms(OMS(id="omsBC", src_node_id="B", dst_node_id="C",
                  elements=("a2", "fBC", "a3")))
    n.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",),
                              mode_id="200G-16QAM", center_freq_hz=193.4e12))
    n.add_lightpath(Lightpath(id="lpBC", oms_sequence=("omsBC",),
                              mode_id="200G-16QAM", center_freq_hz=193.4e12))
    for rid, site in (("R-A", "A"), ("R-B", "B"), ("R-C", "C")):
        n.add_router(Router(id=rid, site=site))
    n.add_ip_link(IPLink(id="ipAB", a_router="R-A", z_router="R-B",
                         lightpath_id="lpAB"))
    n.add_ip_link(IPLink(id="ipBC", a_router="R-B", z_router="R-C",
                         lightpath_id="lpBC"))
    # Healthy QoT on both lightpaths (200G feasible, margin positive).
    n.set_qot_state("lpAB", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=3.5))
    n.set_qot_state("lpBC", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=3.5))
    return n


def test_ip_links_for_lightpath_reverse_lookup():
    n = _two_link_model()
    assert n.ip_links_for_lightpath("lpAB") == ("ipAB",)
    assert n.ip_links_for_lightpath("lpBC") == ("ipBC",)


def test_ip_links_for_lightpath_unknown_raises():
    n = _two_link_model()
    with pytest.raises(KeyError):
        n.ip_links_for_lightpath("nope")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/model/test_ip_routing.py::test_ip_links_for_lightpath_reverse_lookup -v`
Expected: FAIL — `AttributeError: 'NetworkModel' object has no attribute 'ip_links_for_lightpath'`.

- [ ] **Step 3: Implement on `NetworkModel`**

Add after `list_ip_links` (around `network.py:122`):

```python
    def ip_links_for_lightpath(self, lp_id: str) -> Tuple[str, ...]:
        """Coupling #1 reverse lookup: the IP link(s) bound to a lightpath.
        Raises KeyError if the lightpath is unknown."""
        if lp_id not in self._lightpaths:
            raise KeyError(lp_id)
        return tuple(
            link.id for link in self._ip_links.values()
            if link.lightpath_id == lp_id
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/model/test_ip_routing.py -v`
Expected: PASS (both new tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py tests/model/test_ip_routing.py
git commit -m "feat(model): coupling-1 reverse lookup ip_links_for_lightpath"
```

---

## Task 2: Coupling #3 — first-class grooming map

**Files:**
- Create: `src/multilayer_optical_mcp/model/ip_routing.py`
- Test: `tests/model/test_ip_routing.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/model/test_ip_routing.py
from multilayer_optical_mcp.model.assets import Service as _S  # noqa: F401
from multilayer_optical_mcp.model import ip_routing


def test_grooming_map_both_directions():
    n = _two_link_model()
    # One service A->C rides both lightpaths; one service A->B rides only lpAB.
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    n.add_service(Service(id="svc-AB", src_router="R-A", dst_router="R-B",
                          demand_gbps=40.0, working_path=("ipAB",)))
    gm = ip_routing.build_grooming_map(n)
    # by_service: each service -> the lightpaths along its working_path
    assert gm.by_service["svc-AC"] == ("lpAB", "lpBC")
    assert gm.by_service["svc-AB"] == ("lpAB",)
    # by_lightpath: each lightpath -> the services riding it (sorted)
    assert gm.by_lightpath["lpAB"] == ("svc-AB", "svc-AC")
    assert gm.by_lightpath["lpBC"] == ("svc-AC",)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/model/test_ip_routing.py::test_grooming_map_both_directions -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'multilayer_optical_mcp.model.ip_routing'`.

- [ ] **Step 3: Create `model/ip_routing.py` with the grooming map**

```python
# src/multilayer_optical_mcp/model/ip_routing.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Tuple
from .network import NetworkModel


@dataclass(frozen=True)
class GroomingMap:
    """Coupling #3, both directions, derived from services (single source of
    truth). `by_service`: service id -> lightpaths its working_path rides, in
    path order. `by_lightpath`: lightpath id -> services riding it, sorted."""
    by_service: Dict[str, Tuple[str, ...]]
    by_lightpath: Dict[str, Tuple[str, ...]]


def build_grooming_map(model: NetworkModel) -> GroomingMap:
    by_service: Dict[str, Tuple[str, ...]] = {}
    rev: Dict[str, list] = {}
    for svc in model.list_services():
        lps = tuple(model.get_ip_link(ip).lightpath_id for ip in svc.working_path)
        by_service[svc.id] = lps
        for lp in lps:
            rev.setdefault(lp, [])
            if svc.id not in rev[lp]:
                rev[lp].append(svc.id)
    by_lightpath = {lp: tuple(sorted(svcs)) for lp, svcs in rev.items()}
    return GroomingMap(by_service=by_service, by_lightpath=by_lightpath)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/model/test_ip_routing.py::test_grooming_map_both_directions -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/ip_routing.py tests/model/test_ip_routing.py
git commit -m "feat(ip): first-class grooming map (by_service + by_lightpath)"
```

---

## Task 3: `simulate_ip_routing` — offered load, utilizations, drops

**Files:**
- Modify: `src/multilayer_optical_mcp/model/ip_routing.py`
- Test: `tests/model/test_ip_routing.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/model/test_ip_routing.py
def test_simulate_offered_load_and_utilization():
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    n.add_service(Service(id="svc-AB", src_router="R-A", dst_router="R-B",
                          demand_gbps=40.0, working_path=("ipAB",)))
    res = ip_routing.simulate_ip_routing(n)
    util = {u.ip_link_id: u for u in res.utilizations}
    # ipAB carries 120 + 40 = 160 of 200 -> util 0.8; ipBC carries 120 -> 0.6
    assert util["ipAB"].offered_gbps == 160.0
    assert util["ipAB"].capacity_gbps == 200.0
    assert util["ipAB"].utilization == pytest.approx(0.8)
    assert util["ipBC"].utilization == pytest.approx(0.6)
    assert res.congested_links == ()
    assert res.down_links == ()
    assert res.dropped_services == ()
    assert res.overflow_gbps == 0.0


def test_simulate_unrouted_service_carries_no_load():
    n = _two_link_model()
    n.add_service(Service(id="svc-pending", src_router="R-A", dst_router="R-C",
                          demand_gbps=999.0))  # empty working_path
    res = ip_routing.simulate_ip_routing(n)
    assert all(u.offered_gbps == 0.0 for u in res.utilizations)


def test_simulate_oversubscription_reports_overflow():
    n = _two_link_model()
    # 260G offered onto a 200G link -> congested, overflow 60, not down.
    n.add_service(Service(id="svc-big", src_router="R-A", dst_router="R-B",
                          demand_gbps=260.0, working_path=("ipAB",)))
    res = ip_routing.simulate_ip_routing(n)
    util = {u.ip_link_id: u for u in res.utilizations}
    assert util["ipAB"].utilization == pytest.approx(1.3)
    assert res.congested_links == ("ipAB",)
    assert res.down_links == ()
    assert res.overflow_gbps == pytest.approx(60.0)
    assert res.dropped_services == ()  # not down: congested, not lost


def test_simulate_down_link_drops_its_services():
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    # Push lpBC margin negative -> capacity 0 -> ipBC down.
    n.set_qot_state("lpBC", QoTState(gsnr_db=18.0, osnr_db=20.0, margin_db=-0.5))
    res = ip_routing.simulate_ip_routing(n)
    util = {u.ip_link_id: u for u in res.utilizations}
    assert util["ipBC"].down is True
    assert util["ipBC"].utilization is None
    assert res.down_links == ("ipBC",)
    assert res.dropped_services == (
        ip_routing.DroppedService(service_id="svc-AC", reason="link_down",
                                  on_link="ipBC"),
    )
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/model/test_ip_routing.py -k simulate -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'simulate_ip_routing'`.

- [ ] **Step 3: Implement offered-load + simulate in `model/ip_routing.py`**

Add the imports/types/functions:

```python
# add to the imports at the top of model/ip_routing.py
from typing import Dict, List, Optional, Tuple


def offered_load_per_link(model: NetworkModel) -> Dict[str, float]:
    """Sum each routed service's demand onto every IP link in its pinned
    working_path. Empty working_path => unrouted, carries no load. Protection
    paths are standby and contribute zero (decision 3)."""
    load: Dict[str, float] = {link.id: 0.0 for link in model.list_ip_links()}
    for svc in model.list_services():
        for ip_id in svc.working_path:
            load[ip_id] += svc.demand_gbps
    return load


@dataclass(frozen=True)
class LinkUtilization:
    ip_link_id: str
    offered_gbps: float
    capacity_gbps: float
    utilization: Optional[float]   # offered/capacity; None when the link is down
    down: bool                     # capacity == 0 (margin-negative or torn down)


@dataclass(frozen=True)
class DroppedService:
    service_id: str
    reason: str                    # currently always "link_down"
    on_link: str


@dataclass(frozen=True)
class IPRoutingResult:
    utilizations: Tuple[LinkUtilization, ...]
    congested_links: Tuple[str, ...]      # utilization > 1, not down
    down_links: Tuple[str, ...]           # capacity 0 with load > 0
    dropped_services: Tuple[DroppedService, ...]
    overflow_gbps: float                  # Σ max(0, offered-cap) over congested links


def simulate_ip_routing(model: NetworkModel) -> IPRoutingResult:
    """Pure read: account pinned working_path demand onto IP links and report
    utilization, congestion, and drops. Routes nothing (decision 1)."""
    load = offered_load_per_link(model)
    utils: List[LinkUtilization] = []
    congested: List[str] = []
    down: List[str] = []
    overflow = 0.0
    capacity: Dict[str, float] = {}
    for link in model.list_ip_links():
        offered = load[link.id]
        cap = model.ip_link_capacity_gbps(link.id)
        capacity[link.id] = cap
        is_down = cap == 0.0
        util = None if is_down else offered / cap
        utils.append(LinkUtilization(link.id, offered, cap, util, is_down))
        if is_down:
            if offered > 0.0:
                down.append(link.id)
        elif util is not None and util > 1.0:
            congested.append(link.id)
            overflow += offered - cap
    down_set = set(down)
    dropped: List[DroppedService] = []
    for svc in model.list_services():
        for ip_id in svc.working_path:
            if ip_id in down_set:
                dropped.append(DroppedService(svc.id, "link_down", ip_id))
                break  # the service is fully lost once any link on it is down
    return IPRoutingResult(
        utilizations=tuple(utils),
        congested_links=tuple(congested),
        down_links=tuple(down),
        dropped_services=tuple(dropped),
        overflow_gbps=overflow,
    )
```

> Note: iteration order is `list_ip_links()` / `list_services()` insertion order (deterministic), so `congested_links`, `down_links`, and `dropped_services` are stable without sorting.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/model/test_ip_routing.py -v`
Expected: PASS (all simulate, grooming, and reverse-lookup tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/ip_routing.py tests/model/test_ip_routing.py
git commit -m "feat(ip): simulate_ip_routing (pure read; util/congestion/drops)"
```

---

## Task 4: Headline proof — downshift on a branch propagates to congestion

**Files:**
- Create: `tests/model/test_layer_consistency.py`

This is the Step-5 acceptance criterion from `CLAUDE.md`: *"Prove a downshift on a branch propagates to reduced IP capacity and shows congestion."* No production code — it exercises Tasks 1–3 plus the existing capacity gate and COW branch. If it fails, a coupling is wrong.

- [ ] **Step 1: Write the test**

```python
# tests/model/test_layer_consistency.py
import pytest
from multilayer_optical_mcp.model.snapshots import SnapshotStore
from multilayer_optical_mcp.model.assets import Service
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model import ip_routing
from tests.model.test_ip_routing import _two_link_model


def _seeded_store():
    n = _two_link_model()
    # 150G A->B on a 200G link: healthy, util 0.75, no congestion.
    n.add_service(Service(id="svc-AB", src_router="R-A", dst_router="R-B",
                          demand_gbps=150.0, working_path=("ipAB",)))
    store = SnapshotStore(initial=n)
    return store


def test_downshift_on_branch_creates_congestion_and_leaves_truth_intact():
    store = _seeded_store()
    base = store.create()

    # Baseline (ground truth): 200G capacity, util 0.75, healthy.
    base_res = ip_routing.simulate_ip_routing(store.get(base))
    base_util = {u.ip_link_id: u for u in base_res.utilizations}["ipAB"]
    assert base_util.capacity_gbps == 200.0
    assert base_util.utilization == pytest.approx(0.75)
    assert base_res.congested_links == ()

    # Branch and downshift 16QAM -> QPSK on the branch only.
    branch = store.branch(base)
    bm = store.get(branch)
    bm.set_lightpath_mode("lpAB", "100G-QPSK")
    # Single-transponder-type network => GSNR unchanged; only the mode's
    # required threshold changes. QPSK threshold (12 dB) < achieved 22 dB, so
    # margin stays positive and capacity follows the new mode to 100G.
    bm.set_qot_state("lpAB", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=10.0))

    branch_res = ip_routing.simulate_ip_routing(bm)
    branch_util = {u.ip_link_id: u for u in branch_res.utilizations}["ipAB"]
    assert branch_util.capacity_gbps == 100.0          # halved, derived from mode
    assert branch_util.utilization == pytest.approx(1.5)
    assert branch_res.congested_links == ("ipAB",)     # 150 on 100 -> congested
    assert branch_res.overflow_gbps == pytest.approx(50.0)

    # Ground truth untouched: re-simulate the base snapshot.
    again = ip_routing.simulate_ip_routing(store.get(base))
    assert {u.ip_link_id: u for u in again.utilizations}["ipAB"].capacity_gbps == 200.0
    assert again.congested_links == ()


def test_margin_negative_on_branch_drops_the_service():
    store = _seeded_store()
    base = store.create()
    branch = store.branch(base)
    bm = store.get(branch)
    # Degradation pushes lpAB margin negative -> ipAB down -> svc-AB dropped.
    bm.set_qot_state("lpAB", QoTState(gsnr_db=17.0, osnr_db=19.0, margin_db=-1.0))
    res = ip_routing.simulate_ip_routing(bm)
    assert res.down_links == ("ipAB",)
    assert res.dropped_services == (
        ip_routing.DroppedService("svc-AB", "link_down", "ipAB"),
    )
    # Base snapshot still healthy.
    assert ip_routing.simulate_ip_routing(store.get(base)).down_links == ()
```

- [ ] **Step 2: Run**

Run: `python -m pytest tests/model/test_layer_consistency.py -v`
Expected: PASS. If `capacity_gbps` does not halve, coupling #2 is mis-wired; if the base snapshot shows congestion, COW branching leaked.

- [ ] **Step 3: Commit**

```bash
git add tests/model/test_layer_consistency.py
git commit -m "test(ip): prove downshift/margin-negative on a branch propagates to IP"
```

---

# Part B — expose the IP tools

## Task 5: `reroute_service` model mutation + path contiguity

**Files:**
- Modify: `src/multilayer_optical_mcp/model/ip_routing.py` (contiguity helper)
- Modify: `src/multilayer_optical_mcp/model/network.py` (`set_service_working_path`)
- Test: `tests/model/test_ip_routing.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/model/test_ip_routing.py
def test_reroute_repins_working_path():
    n = _two_link_model()
    # Add a direct A->C express link so a reroute target exists.
    n.add_lightpath(Lightpath(id="lpAC", oms_sequence=("omsAB", "omsBC"),
                              mode_id="200G-16QAM", center_freq_hz=193.5e12))
    n.set_qot_state("lpAC", QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=1.0))
    n.add_ip_link(IPLink(id="ipAC", a_router="R-A", z_router="R-C",
                         lightpath_id="lpAC"))
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    n.set_service_working_path("svc-AC", ("ipAC",))
    assert n.get_service("svc-AC").working_path == ("ipAC",)
    # Load now lands on ipAC, not the old two-hop path.
    load = ip_routing.offered_load_per_link(n)
    assert load["ipAC"] == 120.0
    assert load["ipAB"] == 0.0


def test_reroute_rejects_noncontiguous_path():
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    # ipAB alone goes A->B, not A->C.
    with pytest.raises(ValueError, match="does not connect"):
        n.set_service_working_path("svc-AC", ("ipAB",))


def test_reroute_rejects_unknown_link():
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    with pytest.raises(ValueError, match="unknown IP link"):
        n.set_service_working_path("svc-AC", ("ipNope",))
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/model/test_ip_routing.py -k reroute -v`
Expected: FAIL — `AttributeError: 'NetworkModel' object has no attribute 'set_service_working_path'`.

- [ ] **Step 3: Add the contiguity helper to `model/ip_routing.py`**

```python
def is_contiguous_path(
    model: NetworkModel, src_router: str, dst_router: str,
    ip_path: Tuple[str, ...],
) -> bool:
    """True iff ip_path forms a connected walk src_router -> dst_router,
    traversing each IP link in either orientation. Empty path is contiguous
    only when src == dst. Assumes every link id exists (validate first)."""
    cur = src_router
    for ip_id in ip_path:
        link = model.get_ip_link(ip_id)
        if link.a_router == cur:
            cur = link.z_router
        elif link.z_router == cur:
            cur = link.a_router
        else:
            return False
    return cur == dst_router
```

- [ ] **Step 4: Add `set_service_working_path` to `NetworkModel`**

In `network.py`, ensure `replace` is imported (it already is: `from dataclasses import replace`). Add after `add_service`/`get_service` (around `network.py:136`):

```python
    def set_service_working_path(
        self, service_id: str, ip_path: Tuple[str, ...],
    ) -> None:
        """Re-pin a service's working_path (the reroute_service mutation).
        Validates link existence and src->dst contiguity. The validate/commit
        gate is Step 7; here this mutates the (branch) model directly."""
        from .ip_routing import is_contiguous_path
        svc = self._services[service_id]
        for ip_id in ip_path:
            if ip_id not in self._ip_links:
                raise ValueError(f"unknown IP link {ip_id!r}")
        if not is_contiguous_path(self, svc.src_router, svc.dst_router, ip_path):
            raise ValueError(
                f"ip_path does not connect {svc.src_router!r}->{svc.dst_router!r}"
            )
        self._services[service_id] = replace(svc, working_path=tuple(ip_path))
```

> The import is function-local to avoid a module import cycle (`ip_routing` imports `network`).

- [ ] **Step 5: Run to verify they pass**

Run: `python -m pytest tests/model/test_ip_routing.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py src/multilayer_optical_mcp/model/ip_routing.py tests/model/test_ip_routing.py
git commit -m "feat(ip): reroute_service repins working_path with contiguity check"
```

---

## Task 6: `affected_services` reverse lookup (layer-agnostic)

**Files:**
- Modify: `src/multilayer_optical_mcp/model/ip_routing.py`
- Test: `tests/model/test_ip_routing.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/model/test_ip_routing.py
def test_affected_services_by_lightpath_oms_and_fiber():
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    n.add_service(Service(id="svc-AB", src_router="R-A", dst_router="R-B",
                          demand_gbps=40.0, working_path=("ipAB",)))
    # By lightpath id.
    assert ip_routing.affected_services(n, "lpAB") == ("svc-AB", "svc-AC")
    assert ip_routing.affected_services(n, "lpBC") == ("svc-AC",)
    # By OMS id and by fiber uid (deeper layers) resolve through the lightpath.
    assert ip_routing.affected_services(n, "omsBC") == ("svc-AC",)
    assert ip_routing.affected_services(n, "fBC") == ("svc-AC",)
    # By IP link id.
    assert ip_routing.affected_services(n, "ipAB") == ("svc-AB", "svc-AC")
    # Unknown asset -> empty, not an error.
    assert ip_routing.affected_services(n, "ghost") == ()


def test_affected_services_includes_protection_path():
    n = _two_link_model()
    n.add_lightpath(Lightpath(id="lpAC", oms_sequence=("omsAB", "omsBC"),
                              mode_id="200G-16QAM", center_freq_hz=193.5e12))
    n.set_qot_state("lpAC", QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=1.0))
    n.add_ip_link(IPLink(id="ipAC", a_router="R-A", z_router="R-C",
                         lightpath_id="lpAC"))
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAC",),
                          protection_path=("ipAB", "ipBC")))
    # lpBC only on the protection path -> still affected.
    assert ip_routing.affected_services(n, "lpBC") == ("svc-AC",)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/model/test_ip_routing.py -k affected -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'affected_services'`.

- [ ] **Step 3: Implement in `model/ip_routing.py`**

```python
def affected_services(model: NetworkModel, asset_id: str) -> Tuple[str, ...]:
    """Reverse lookup: services whose working OR protection path touches
    asset_id, where asset_id may be an IP link, lightpath, OMS, or
    fiber/amp/roadm uid. Reuses the multilayer asset-set expansion from
    exposure.py so the membership test is layer-agnostic. Unknown asset ->
    empty tuple (not an error)."""
    from .exposure import _path_asset_set
    hits = []
    for svc in model.list_services():
        footprint = (_path_asset_set(model, svc.working_path)
                     | _path_asset_set(model, svc.protection_path))
        if asset_id in footprint:
            hits.append(svc.id)
    return tuple(sorted(hits))
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/model/test_ip_routing.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/ip_routing.py tests/model/test_ip_routing.py
git commit -m "feat(ip): affected_services layer-agnostic reverse lookup"
```

---

## Task 7: Serializers in `model/views.py`

**Files:**
- Modify: `src/multilayer_optical_mcp/model/views.py`
- Test: `tests/model/test_views.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/model/test_views.py
from multilayer_optical_mcp.model.assets import Service
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model import views
from tests.model.test_ip_routing import _two_link_model


def _model_with_services():
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    return n


def test_ip_topology_dict_annotates_capacity_and_load():
    n = _model_with_services()
    d = views.ip_topology_dict(n)
    links = {l["id"]: l for l in d["ip_links"]}
    assert links["ipAB"]["lightpath_id"] == "lpAB"
    assert links["ipAB"]["capacity_gbps"] == 200.0
    assert links["ipAB"]["load_gbps"] == 120.0
    assert {r["id"] for r in d["routers"]} == {"R-A", "R-B", "R-C"}


def test_ip_topology_dict_capacity_null_when_no_qot():
    n = _two_link_model()
    # Wipe one lightpath's QoT so capacity is unknown, not a crash.
    n._qot_state.pop("lpAB")
    d = views.ip_topology_dict(n)
    links = {l["id"]: l for l in d["ip_links"]}
    assert links["ipAB"]["capacity_gbps"] is None


def test_grooming_map_dict_both_directions():
    n = _model_with_services()
    d = views.grooming_map_dict(n)
    assert d["by_service"]["svc-AC"] == ["lpAB", "lpBC"]
    assert d["by_lightpath"]["lpAB"] == ["svc-AC"]


def test_ip_routing_result_dict_shape():
    n = _model_with_services()
    from multilayer_optical_mcp.model import ip_routing
    d = views.ip_routing_result_dict(ip_routing.simulate_ip_routing(n))
    assert set(d) == {"utilizations", "congestion", "dropped"}
    u = {x["ip_link_id"]: x for x in d["utilizations"]}
    assert u["ipAB"]["utilization"] == 0.6
    assert d["congestion"] == []
    assert d["dropped"]["services"] == []
    assert d["dropped"]["overflow_gbps"] == 0.0
    assert d["dropped"]["down_links"] == []


def test_affected_services_dict_shape():
    n = _model_with_services()
    d = views.affected_services_dict(n, "lpBC")
    assert d == {"asset_id": "lpBC", "services": ["svc-AC"]}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/model/test_views.py -k "ip_topology or grooming or ip_routing or affected" -v`
Expected: FAIL — `AttributeError: module 'views' has no attribute 'ip_topology_dict'`.

- [ ] **Step 3: Implement the serializers in `model/views.py`**

Add at the top with the other imports:

```python
from . import ip_routing as _ipr
```

Append these functions:

```python
def ip_topology_dict(model: NetworkModel) -> Dict[str, Any]:
    """get_ip_topology: routers + IP links, each annotated with its underlying
    lightpath, derived capacity (margin-gated; None if no QoT recorded yet),
    and current offered load. Read-only."""
    load = _ipr.offered_load_per_link(model)
    links = []
    for link in model.list_ip_links():
        try:
            cap = model.ip_link_capacity_gbps(link.id)
        except LookupError:
            cap = None
        links.append({
            "id": link.id,
            "a_router": link.a_router,
            "z_router": link.z_router,
            "lightpath_id": link.lightpath_id,
            "capacity_gbps": cap,
            "load_gbps": load[link.id],
        })
    return {
        "routers": [_router(r) for r in model._routers.values()],
        "ip_links": links,
    }


def grooming_map_dict(model: NetworkModel) -> Dict[str, Any]:
    gm = _ipr.build_grooming_map(model)
    return {
        "by_service": {sid: list(lps) for sid, lps in gm.by_service.items()},
        "by_lightpath": {lp: list(svcs) for lp, svcs in gm.by_lightpath.items()},
    }


def ip_routing_result_dict(res) -> Dict[str, Any]:
    """Serialize IPRoutingResult to the CLAUDE.md {utilizations, congestion,
    dropped} shape. utilization is null for a down link."""
    return {
        "utilizations": [
            {
                "ip_link_id": u.ip_link_id,
                "offered_gbps": u.offered_gbps,
                "capacity_gbps": u.capacity_gbps,
                "utilization": u.utilization,
                "down": u.down,
            }
            for u in res.utilizations
        ],
        "congestion": list(res.congested_links),
        "dropped": {
            "services": [
                {"service_id": d.service_id, "reason": d.reason,
                 "on_link": d.on_link}
                for d in res.dropped_services
            ],
            "down_links": list(res.down_links),
            "overflow_gbps": res.overflow_gbps,
        },
    }


def affected_services_dict(model: NetworkModel, asset_id: str) -> Dict[str, Any]:
    return {"asset_id": asset_id,
            "services": list(_ipr.affected_services(model, asset_id))}
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/model/test_views.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/views.py tests/model/test_views.py
git commit -m "feat(views): ip_topology/grooming/ip_routing/affected serializers"
```

---

## Task 8: Expose the five tools on the server

**Files:**
- Modify: `src/multilayer_optical_mcp/server.py`
- Test: `tests/test_server_phase5.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_phase5.py
import pytest
from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, Lightpath, Router, IPLink, Service,
)
from multilayer_optical_mcp.model.qot import QoTState


def _seed(app):
    """Populate the app's current model with the A->B->C two-link network."""
    n = app._snapshots.current()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for nid in ("a1", "a2", "a3"):
        n.add_amplifier(Amplifier(id=nid, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0,
                      type_variety="SSMF"))
    n.add_fiber(Fiber(id="fBC", a_end="a2", z_end="a3", length_km=80.0,
                      type_variety="SSMF"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B",
                  elements=("a1", "fAB", "a2")))
    n.add_oms(OMS(id="omsBC", src_node_id="B", dst_node_id="C",
                  elements=("a2", "fBC", "a3")))
    # modes come from modulation_formats.yaml; pick the first available id.
    mode_id = app._snapshots.current().modes.list()[0].id
    n.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",),
                              mode_id=mode_id, center_freq_hz=193.4e12))
    n.add_lightpath(Lightpath(id="lpBC", oms_sequence=("omsBC",),
                              mode_id=mode_id, center_freq_hz=193.4e12))
    for rid, site in (("R-A", "A"), ("R-B", "B"), ("R-C", "C")):
        n.add_router(Router(id=rid, site=site))
    n.add_ip_link(IPLink(id="ipAB", a_router="R-A", z_router="R-B",
                         lightpath_id="lpAB"))
    n.add_ip_link(IPLink(id="ipBC", a_router="R-B", z_router="R-C",
                         lightpath_id="lpBC"))
    bitrate = n.modes.list()[0].bitrate_gbps
    n.set_qot_state("lpAB", QoTState(gsnr_db=30.0, osnr_db=32.0, margin_db=5.0))
    n.set_qot_state("lpBC", QoTState(gsnr_db=30.0, osnr_db=32.0, margin_db=5.0))
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=bitrate * 0.5, working_path=("ipAB", "ipBC")))
    return bitrate


def _call(app, name, **kwargs):
    """Invoke a registered FastMCP tool's underlying function directly.
    Mirrors tests/test_server_phase4.py exactly."""
    return app._tool_manager._tools[name].fn(**kwargs)


def test_get_ip_topology_annotates_links():
    app = build_app()
    bitrate = _seed(app)
    d = _call(app, "get_ip_topology")
    links = {l["id"]: l for l in d["ip_links"]}
    assert links["ipAB"]["capacity_gbps"] == bitrate
    assert links["ipAB"]["load_gbps"] == pytest.approx(bitrate * 0.5)


def test_get_grooming_map_tool():
    app = build_app()
    _seed(app)
    d = _call(app, "get_grooming_map")
    assert d["by_service"]["svc-AC"] == ["lpAB", "lpBC"]


def test_get_affected_services_tool():
    app = build_app()
    _seed(app)
    assert _call(app, "get_affected_services", asset_id="lpBC")["services"] == ["svc-AC"]


def test_simulate_ip_routing_tool():
    app = build_app()
    _seed(app)
    d = _call(app, "simulate_ip_routing")
    assert set(d) == {"utilizations", "congestion", "dropped"}
    assert d["congestion"] == []


def test_reroute_service_tool_repins_and_resimulates():
    app = build_app()
    bitrate = _seed(app)
    n = app._snapshots.current()
    # Add a direct express link A->C to reroute onto.
    mode_id = n.modes.list()[0].id
    n.add_lightpath(Lightpath(id="lpAC", oms_sequence=("omsAB", "omsBC"),
                              mode_id=mode_id, center_freq_hz=193.5e12))
    n.set_qot_state("lpAC", QoTState(gsnr_db=30.0, osnr_db=32.0, margin_db=5.0))
    n.add_ip_link(IPLink(id="ipAC", a_router="R-A", z_router="R-C",
                         lightpath_id="lpAC"))
    out = _call(app, "reroute_service", service_id="svc-AC", ip_path=["ipAC"])
    assert out["service_id"] == "svc-AC"
    assert out["working_path"] == ["ipAC"]
    # Load moved: ipAB now idle, ipAC carries the demand.
    topo = {l["id"]: l for l in _call(app, "get_ip_topology")["ip_links"]}
    assert topo["ipAB"]["load_gbps"] == 0.0
    assert topo["ipAC"]["load_gbps"] == pytest.approx(bitrate * 0.5)


def test_reroute_service_tool_rejects_bad_path():
    app = build_app()
    _seed(app)
    with pytest.raises(ValueError, match="does not connect"):
        _call(app, "reroute_service", service_id="svc-AC", ip_path=["ipAB"])
```

> The `_call` helper is the verified pattern from `tests/test_server.py:40` and `tests/test_server_phase4.py:17` — `app._tool_manager._tools[name].fn(**kwargs)` (the tool functions are synchronous). Use it verbatim.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_server_phase5.py -v`
Expected: FAIL — tools `get_ip_topology` / `get_grooming_map` / `get_affected_services` / `simulate_ip_routing` / `reroute_service` are not registered.

- [ ] **Step 3: Register the tools in `server.py`**

Extend the `from .model.views import (...)` block (around `server.py:152`) with:

```python
        ip_topology_dict, grooming_map_dict, ip_routing_result_dict,
        affected_services_dict,
```

Add `from .model.ip_routing import simulate_ip_routing as _simulate_ip_routing` near the other `model.*` imports inside `build_app` (around `server.py:158`).

Add these tools alongside the other read tools (after `get_traffic_matrix`, around `server.py:195`):

```python
    @app.tool()
    def get_ip_topology() -> dict:
        """Routers and IP links; each link annotated with its underlying
        lightpath, derived (margin-gated) capacity, and current offered load."""
        return ip_topology_dict(snapshots.current())

    @app.tool()
    def get_grooming_map() -> dict:
        """Coupling #3: which demands ride which lightpaths, both directions
        (by_service and by_lightpath)."""
        return grooming_map_dict(snapshots.current())

    @app.tool()
    def get_affected_services(asset_id: str) -> dict:
        """Reverse lookup: services whose working or protection path crosses
        asset_id (IP link, lightpath, OMS, or fiber/amp/roadm uid)."""
        return affected_services_dict(snapshots.current(), asset_id)

    @app.tool()
    def simulate_ip_routing() -> dict:
        """Read-only: account pinned working_path demand onto IP links and
        report {utilizations, congestion, dropped}. Routes nothing — a downshift
        or margin-negative lightpath surfaces here as reduced capacity, congestion,
        or dropped services."""
        return ip_routing_result_dict(_simulate_ip_routing(snapshots.current()))

    @app.tool()
    def reroute_service(service_id: str, ip_path: list[str]) -> dict:
        """Move a service's working_path onto a different IP-link sequence over
        survivors. Validates contiguity src->dst; raises on an invalid path.
        Path computation is the caller's job (use compute_paths)."""
        model = snapshots.current()
        model.set_service_working_path(service_id, tuple(ip_path))
        svc = model.get_service(service_id)
        return {"service_id": svc.id, "working_path": list(svc.working_path)}
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_server_phase5.py -v`
Expected: PASS (all six).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/server.py tests/test_server_phase5.py
git commit -m "feat(server): expose get_ip_topology/get_grooming_map/get_affected_services/simulate_ip_routing/reroute_service"
```

---

## Verification

1. `python -m pytest -q` — the existing suite plus all Part A/B tests green.
2. **Determinism:** run `tests/model/test_ip_routing.py` and `tests/model/test_layer_consistency.py` twice → identical results (no set-ordering leaks into `congested_links`/`down_links`/`dropped_services`).
3. **Read tools never mutate:** `get_ip_topology`, `get_grooming_map`, `get_affected_services`, `simulate_ip_routing` leave `snapshot_diff(before, after)` empty.
4. **Coupling proof (the Step-5 acceptance gate):** `tests/model/test_layer_consistency.py` shows a branch downshift halving capacity into congestion while ground truth stays at nominal, and a margin-negative branch dropping the service.
5. **No GNPy required** for any Part A/B test (margin is set directly, mirroring `test_capacity_coupling.py`).

## Out of scope (Step 6 / Step 7)

- **What-if + injection** (`whatif_margin_threshold_sweep`, `inject_degradation`, `inject_failure`) — Step 6.
- **The validate/commit gate** — `validate_plan` (incl. IP-link overload and transient make-before-break overload), `provision_lightpath`/`teardown_lightpath` flipping the bound IP link up/down, `set_modulation_format` as a *tool*, `commit_plan`, `reconcile` — Step 7. `reroute_service` here mutates a branch directly; it is **not** yet behind the approval gate.
- **IGP/ECMP path computation inside `simulate`** — explicitly rejected (decision 1). Path computation stays in the solvers / `reroute_service` caller.
- **Max-flow / fair-share TE drop apportionment** — the link-local overflow model (decision 4) is the intended first cut; no optimal flow placement is claimed.
```