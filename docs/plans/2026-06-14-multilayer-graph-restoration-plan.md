# Multi-Layer Graph + Restoration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-service optical/IP restoration over survivors, built on a new layered IP+optical auxiliary graph, plus an avoidance constraint on the OMS router.

**Architecture:** A read-only `compute_restoration` enumerates recovery candidates for a failed service by routing its demand over a *pruned* layered graph (failed assets removed). The graph (Zhu/Mukherjee auxiliary-graph model, per-wavelength layers, no conversion) reuses existing-lightpath edges (LPE, residual capacity) cheaply and gives new-lightpath edges a moderate weight; QoT realizes new lightpaths after routing. Restoration is k-best placement over the one graph, yielding three lever types — `ip_reroute` (reuse), `optical_reroute` (new), and `hybrid` (both in one path).

**Tech Stack:** Python 3.11, NetworkX, pytest. Conda env `multilayer-optical-mcp` (run all commands via `conda run -n multilayer-optical-mcp ...`, or the env python at `C:/Users/olegk/miniconda3/envs/multilayer-optical-mcp/python.exe`).

**Design doc:** `docs/plans/2026-06-14-multilayer-graph-restoration-design.md`

**Conventions baked in (do not re-derive):**
- `Router.site` is the optical-node id (e.g. `Router(id="R1", site="A")` routes from optical node `"A"`).
- QoT is reached through the `QotEvaluator` protocol in `model/allocation.py`; tests pass a fake (see `tests/model/test_allocation.py::FakeQot`).
- Spectrum occupancy is a per-OMS integer bitmask (`model/spectrum.py`): bit λ set ⇒ slot λ lit on that OMS.
- `model.ip_link_capacity_gbps(link_id)` returns 0.0 when the bound lightpath's recorded QoT `margin_db < 0`, else `mode.bitrate_gbps`. It raises `LookupError` if no QoT state was recorded, so tests must `model.set_qot_state(lp_id, QoTState(...))`.
- Solver outcomes are typed via `SolverStatus` (`solution`/`partial`/`no_solution`), never exceptions.

---

## File Structure

- **Modify** `src/multilayer_optical_mcp/model/solvers.py` — avoidance: resolve a forbidden-OMS/node set from `constraints={"avoid": {...}}` and prune the OMS graph before enumeration (threaded through `_oms_between`).
- **Create** `src/multilayer_optical_mcp/model/multilayer_graph.py` — the layered auxiliary graph builder + `place_demands` (IGABAG k-best placement with QoT realization).
- **Create** `src/multilayer_optical_mcp/model/restoration.py` — `compute_restoration` and its result types.
- **Modify** `src/multilayer_optical_mcp/model/views.py` — `restoration_result_dict` serializer.
- **Modify** `src/multilayer_optical_mcp/server.py` — `compute_restoration` MCP tool.
- **Tests:** `tests/model/test_avoidance.py`, `tests/model/test_multilayer_graph.py`, `tests/model/test_restoration.py`, `tests/test_server_phase8.py`.

---

## Task 1: Avoidance constraint on the OMS router

**Files:**
- Modify: `src/multilayer_optical_mcp/model/solvers.py`
- Test: `tests/model/test_avoidance.py`

The avoid-set resolves to a set of forbidden OMS ids (an OMS is forbidden if any of its assets — its own id, its fiber/amp/roadm elements, or either endpoint node — is in `avoid.assets`, or if any risk group in `avoid.risk_groups` has a member intersecting the OMS's asset set). Pruning happens in `build_oms_graph` and `_oms_between` so parallel OMS in *different* SRLGs are handled per-edge.

- [ ] **Step 1: Write the failing test**

```python
# tests/model/test_avoidance.py
"""Avoidance: route over survivors by pruning forbidden OMS before enumeration.
Reuses the two-parallel-route shape (oms-north / oms-south, A->B)."""
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, SRLG, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.solvers import SolverStatus, compute_paths


def _two_parallel() -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9)]))
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("aN1", "aN2", "aS1", "aS2"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber("fiber-north", "aN1", "aN2", 80.0, "SSMF"))
    n.add_fiber(Fiber("fiber-south", "aS1", "aS2", 80.0, "SSMF"))
    n.add_oms(OMS("oms-north", "A", "B", ("aN1", "fiber-north", "aN2")))
    n.add_oms(OMS("oms-south", "A", "B", ("aS1", "fiber-south", "aS2")))
    return n


def test_avoid_prunes_forbidden_oms_keeps_survivor():
    n = _two_parallel()
    res = compute_paths(n, "A", "B", k=4,
                        constraints={"avoid": {"assets": ["fiber-north"]}})
    assert res.status is SolverStatus.SOLUTION
    found = {p.oms_sequence for p in res.paths}
    assert found == {("oms-south",)}            # north pruned, south survives


def test_avoid_parallel_in_different_srlg_keeps_the_other():
    """Pruning is per-OMS-edge: avoiding an SRLG that contains only the north
    fiber must not remove the south parallel."""
    n = _two_parallel()
    n.add_srlg(SRLG(id="srlg-north", asset_ids=("fiber-north",)))
    res = compute_paths(n, "A", "B", k=4,
                        constraints={"avoid": {"risk_groups": ["srlg-north"]}})
    found = {p.oms_sequence for p in res.paths}
    assert found == {("oms-south",)}


def test_avoid_all_routes_is_typed_no_solution():
    n = _two_parallel()
    res = compute_paths(n, "A", "B", k=4,
                        constraints={"avoid": {"assets": ["fiber-north", "fiber-south"]}})
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.paths == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_avoidance.py -v`
Expected: FAIL — `constraints={"avoid":...}` is currently ignored, so `test_avoid_prunes_forbidden_oms_keeps_survivor` finds both routes.

- [ ] **Step 3: Add the forbidden-OMS resolver and thread pruning through enumeration**

In `src/multilayer_optical_mcp/model/solvers.py`, add the import near the top:

```python
from .exposure import path_basis_keys, split_shared_keys, oms_seq_asset_set
```

Add this helper after `oms_length_km`:

```python
def _avoid_sets(constraints: Optional[dict]) -> Tuple[frozenset, frozenset]:
    """Extract (avoid_assets, avoid_risk_groups) from a constraints dict.
    Missing/empty -> empty sets (no pruning)."""
    avoid = (constraints or {}).get("avoid") or {}
    return frozenset(avoid.get("assets", ())), frozenset(avoid.get("risk_groups", ()))


def forbidden_oms(
    model: NetworkModel, avoid_assets: frozenset, avoid_risk_groups: frozenset,
) -> frozenset:
    """OMS ids to prune: an OMS is forbidden if any of its assets (own id,
    fiber/amp/roadm elements, or either endpoint node) is in avoid_assets, or if
    a risk group / SRLG named in avoid_risk_groups has a member intersecting the
    OMS's physical asset set."""
    if not avoid_assets and not avoid_risk_groups:
        return frozenset()
    group_members: set = set()
    for g in list(model.list_srlgs()) + list(model.list_risk_groups()):
        if g.id in avoid_risk_groups:
            group_members.update(g.asset_ids)
    bad: set = set()
    for oms in model.list_oms():
        phys = set(oms_seq_asset_set(model, (oms.id,)))
        phys.add(oms.src_node_id)
        phys.add(oms.dst_node_id)
        if (phys & avoid_assets) or (phys & group_members):
            bad.add(oms.id)
    return frozenset(bad)
```

- [ ] **Step 4: Prune in `build_oms_graph` and `_oms_between`**

Change `build_oms_graph` and `_oms_between` to accept a `forbidden` set:

```python
def build_oms_graph(model: NetworkModel, forbidden: frozenset = frozenset()) -> nx.MultiGraph:
    g: nx.MultiGraph = nx.MultiGraph()
    for oms in model.list_oms():
        if oms.id in forbidden:
            continue
        g.add_node(oms.src_node_id)
        g.add_node(oms.dst_node_id)
        g.add_edge(oms.src_node_id, oms.dst_node_id, key=oms.id, oms_id=oms.id)
    return g
```

```python
def _oms_between(
    model: NetworkModel, u: str, v: str, *, by_length: bool = False,
    forbidden: frozenset = frozenset(),
) -> List[str]:
    out = [oms.id for oms in model.list_oms()
           if {oms.src_node_id, oms.dst_node_id} == {u, v} and oms.id not in forbidden]
    if by_length:
        return sorted(out, key=lambda o: (oms_length_km(model, o), o))
    return sorted(out)
```

- [ ] **Step 5: Thread `forbidden` through `_enumerate_oms_paths`, `compute_paths`, `compute_disjoint_paths`**

In `_enumerate_oms_paths`, accept `forbidden` and use it everywhere a graph or `_oms_between` is built:

```python
def _enumerate_oms_paths(
    model: NetworkModel, src: str, dst: str, k: int, weight: str = "hops",
    forbidden: frozenset = frozenset(),
) -> Iterator[OmsPath]:
    g = build_oms_graph(model, forbidden)
    if src not in g or dst not in g or src == dst:
        return
    by_length = weight == "length"
    simple = nx.Graph()
    simple.add_nodes_from(g.nodes)
    for u, v in g.edges():
        if by_length:
            w = min(oms_length_km(model, o) for o in _oms_between(model, u, v, forbidden=forbidden))
            if simple.has_edge(u, v):
                simple[u][v]["weight"] = min(simple[u][v]["weight"], w)
            else:
                simple.add_edge(u, v, weight=w)
        else:
            simple.add_edge(u, v)
    emitted = 0
    try:
        node_paths = nx.shortest_simple_paths(
            simple, src, dst, weight="weight" if by_length else None)
    except nx.NetworkXNoPath:
        return
    for node_path in node_paths:
        hop_options = [_oms_between(model, u, v, by_length=by_length, forbidden=forbidden)
                       for u, v in zip(node_path, node_path[1:])]
        for combo in itertools.product(*hop_options):
            yield OmsPath(node_sequence=tuple(node_path), oms_sequence=tuple(combo))
            emitted += 1
            if emitted >= k:
                return
```

In `compute_paths`, resolve and pass `forbidden`:

```python
def compute_paths(
    model: NetworkModel, src: str, dst: str, k: int,
    constraints: Optional[dict] = None, weight: str = "hops",
) -> RoutingResult:
    avoid_assets, avoid_rgs = _avoid_sets(constraints)
    forbidden = forbidden_oms(model, avoid_assets, avoid_rgs)
    paths = tuple(_enumerate_oms_paths(model, src, dst, k, weight=weight, forbidden=forbidden))
    if not paths:
        return RoutingResult(status=SolverStatus.NO_SOLUTION, paths=())
    return RoutingResult(status=SolverStatus.SOLUTION, paths=paths)
```

In `compute_disjoint_paths`, add a `constraints` parameter and pass `forbidden` into the candidate enumeration:

```python
def compute_disjoint_paths(
    model: NetworkModel, src: str, dst: str,
    basis: str, level: str, best_effort: bool = False, weight: str = "hops",
    constraints: Optional[dict] = None,
) -> DisjointnessResult:
    avoid_assets, avoid_rgs = _avoid_sets(constraints)
    forbidden = forbidden_oms(model, avoid_assets, avoid_rgs)
    cands = list(_enumerate_oms_paths(model, src, dst, _DISJOINT_CANDIDATE_CAP,
                                      weight=weight, forbidden=forbidden))
    # ... rest unchanged ...
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_avoidance.py tests/model/test_solvers.py -v`
Expected: PASS (new avoidance tests + the existing solver tests, which call `compute_paths` with no constraints and must be unaffected).

- [ ] **Step 7: Commit**

```bash
git add src/multilayer_optical_mcp/model/solvers.py tests/model/test_avoidance.py
git commit -m "feat(solvers): avoidance constraint prunes forbidden OMS before enumeration"
```

---

## Task 2: Layered graph construction

**Files:**
- Create: `src/multilayer_optical_mcp/model/multilayer_graph.py`
- Test: `tests/model/test_multilayer_graph.py`

A directed graph. Access vertices `(node, "access")`. Existing lightpaths become LPE edges `access(u) → access(v)` carrying `lightpath_id` and residual capacity (absent when margin<0 or residual 0, or when the lightpath crosses a forbidden asset). Per wavelength λ, vertices `(node, "wl", λ)` with WLE edges from free OMS, plus TxE `access(u) → (u,"wl",λ)` and RxE `(v,"wl",λ) → access(v)`. Weights: LPE small, WLE small, TxE large (new-lightpath penalty).

- [ ] **Step 1: Write the failing test**

```python
# tests/model/test_multilayer_graph.py
"""Layered auxiliary graph: existing lightpaths -> LPE edges (residual,
margin-gated); free wavelengths -> WLE edges driven from the OMS bitmask."""
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, Router, IPLink, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.spectrum import SpectrumGrid
from multilayer_optical_mcp.model.multilayer_graph import (
    build_layered_graph, ACCESS, WL, lpe_edges, wle_count_on_layer,
)


def _one_lightpath_model(margin_db: float = 3.0) -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=100e9)]))
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("a1", "a2"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber("fAB", "a1", "a2", 80.0, "SSMF"))
    n.add_oms(OMS("oms-AB", "A", "B", ("a1", "fAB", "a2")))
    # Lightpath on slot 20 (193.4 THz on the default grid).
    n.add_lightpath(Lightpath("lp-AB", ("oms-AB",), "100G", 193.4e12))
    n.set_qot_state("lp-AB", QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=margin_db))
    n.add_router(Router("R1", "A"))
    n.add_router(Router("R2", "B"))
    n.add_ip_link(IPLink("ip-AB", "R1", "R2", "lp-AB"))
    return n


def test_existing_lightpath_becomes_lpe_with_residual():
    n = _one_lightpath_model()
    g = build_layered_graph(n)
    edges = lpe_edges(g)
    assert len(edges) == 1
    (u, v, data), = edges
    assert u == (ACCESS, "A") and v == (ACCESS, "B")
    assert data["lightpath_id"] == "lp-AB"
    assert data["residual_gbps"] == 100.0     # 100G mode, no load


def test_margin_negative_lightpath_has_no_lpe_edge():
    n = _one_lightpath_model(margin_db=-1.0)   # down -> capacity 0
    g = build_layered_graph(n)
    assert lpe_edges(g) == []


def test_wle_present_only_on_free_slots():
    n = _one_lightpath_model()
    grid = SpectrumGrid.default()
    g = build_layered_graph(n)
    # slot 20 is occupied by lp-AB on oms-AB -> no WLE on layer 20
    assert wle_count_on_layer(g, "oms-AB", 20) == 0
    # slot 0 is free -> a WLE exists on layer 0 for oms-AB (both directions)
    assert wle_count_on_layer(g, "oms-AB", 0) == 2


def test_forbidden_asset_drops_lpe_and_wle():
    n = _one_lightpath_model()
    g = build_layered_graph(n, forbidden_assets=frozenset({"fAB"}))
    assert lpe_edges(g) == []                  # lightpath crosses fAB
    assert wle_count_on_layer(g, "oms-AB", 0) == 0   # OMS pruned entirely
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_multilayer_graph.py -v`
Expected: FAIL with `ModuleNotFoundError: multilayer_graph`.

- [ ] **Step 3: Implement the graph builder**

```python
# src/multilayer_optical_mcp/model/multilayer_graph.py
"""Layered IP+optical auxiliary graph (Zhu/Mukherjee model, per-wavelength
layers, no wavelength conversion) + IGABAG single-demand placement.

Vertices:
  (ACCESS, node)        access/IP layer port for an optical node
  (WL, node, lam)       wavelength-layer port for node on slot `lam`

Edges (directed; every edge carries a 'weight'):
  LPE  access(u) -> access(v)   one per existing lightpath u->v.
        Carries lightpath_id + residual_gbps; absent when margin<0, residual 0,
        or the lightpath crosses a forbidden asset. Low weight (reuse).
  WLE  (WL,u,lam) -> (WL,v,lam)  one per free slot `lam` on an OMS u->v (both
        directions); carries oms_id + lam. Low weight.
  TxE  access(u) -> (WL,u,lam)   originate a new lightpath on slot lam. MODERATE
        weight (a few groom-hops' worth) so new segments are discouraged but still
        reachable by k-shortest within budget — letting hybrids interleave into
        the frontier instead of ranking behind every pure-groom path.
  RxE  (WL,v,lam) -> access(v)   terminate a new lightpath. Zero weight.

A path access(src) -> access(dst) that stays on existing lightpaths uses only
LPE edges (grooming). A path that dips via TxE -> WLEs (one lam) -> RxE realizes
a new lightpath on that wavelength. No CvtE: wavelength continuity is structural.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List, Tuple

import networkx as nx

from .network import NetworkModel
from .spectrum import SpectrumGrid, build_spectrum_state
from .exposure import oms_seq_asset_set

ACCESS = "access"
WL = "wl"

# Edge weights shape k-shortest DISCOVERY only (final ranking uses cost_facets).
# New lightpaths are discouraged but reachable, so hybrids/new candidates
# interleave into the frontier rather than ranking behind every pure-groom path
# (which a 1000x penalty would cause). Tunable.
_W_LPE = 1.0       # reuse an existing lightpath (one virtual hop)
_W_WLE = 0.1       # traverse one OMS on a new lightpath's wavelength
_W_NEW_LP = 5.0    # originate a new lightpath (TxE): a few groom-hops' worth
_W_RXE = 0.0


def _lightpath_endpoints(model: NetworkModel, lp) -> Tuple[str, str]:
    """(src_node, dst_node) of a lightpath from its first/last OMS endpoints."""
    first = model.get_oms(lp.oms_sequence[0])
    last = model.get_oms(lp.oms_sequence[-1])
    return first.src_node_id, last.dst_node_id


def _lightpath_forbidden(model: NetworkModel, lp, forbidden_assets: FrozenSet[str]) -> bool:
    if not forbidden_assets:
        return False
    assets = set()
    for oms_id in lp.oms_sequence:
        assets |= oms_seq_asset_set(model, (oms_id,))
        oms = model.get_oms(oms_id)
        assets.add(oms.src_node_id)
        assets.add(oms.dst_node_id)
    return bool(assets & forbidden_assets)


def _residual_gbps(model: NetworkModel, lp) -> float:
    """Derived capacity of the lightpath's bound IP link(s) minus current load.
    A lightpath with no IP link bound yields its full mode rate (margin-gated)."""
    from .ip_routing import offered_load_per_link
    ip_ids = model.ip_links_for_lightpath(lp.id)
    if not ip_ids:
        # no IP link bound: capacity is mode rate iff margin >= 0
        try:
            state = model.get_qot_state(lp.id)
        except LookupError:
            return 0.0
        return 0.0 if state.margin_db < 0 else model.modes.get(lp.mode_id).bitrate_gbps
    load = offered_load_per_link(model)
    residual = 0.0
    for ip_id in ip_ids:
        cap = model.ip_link_capacity_gbps(ip_id)   # 0.0 when margin<0
        residual = max(residual, cap - load.get(ip_id, 0.0))
    return residual


def build_layered_graph(
    model: NetworkModel,
    forbidden_assets: FrozenSet[str] = frozenset(),
    *,
    grid: SpectrumGrid | None = None,
) -> nx.DiGraph:
    """Construct the layered auxiliary graph for the model's current loading.
    `forbidden_assets` prunes any OMS touching them (no WLE) and any lightpath
    crossing them (no LPE)."""
    grid = grid or SpectrumGrid.default()
    g = nx.DiGraph()
    spectrum = build_spectrum_state(model, grid)

    # forbidden OMS: any OMS whose asset set / endpoints intersect forbidden_assets
    def _oms_forbidden(oms) -> bool:
        if not forbidden_assets:
            return False
        phys = set(oms_seq_asset_set(model, (oms.id,)))
        phys.add(oms.src_node_id)
        phys.add(oms.dst_node_id)
        return bool(phys & forbidden_assets)

    # access vertices for every optical node that appears on an OMS endpoint
    for oms in model.list_oms():
        g.add_node((ACCESS, oms.src_node_id))
        g.add_node((ACCESS, oms.dst_node_id))

    # LPE edges: existing lightpaths
    for lp in model.list_lightpaths():
        if _lightpath_forbidden(model, lp, forbidden_assets):
            continue
        residual = _residual_gbps(model, lp)
        if residual <= 0.0:
            continue
        u, v = _lightpath_endpoints(model, lp)
        g.add_edge((ACCESS, u), (ACCESS, v),
                   kind="LPE", lightpath_id=lp.id, residual_gbps=residual,
                   weight=_W_LPE)

    # WLE + TxE/RxE per wavelength layer
    for oms in model.list_oms():
        if _oms_forbidden(oms):
            continue
        occ = spectrum.get(oms.id, 0)
        u, v = oms.src_node_id, oms.dst_node_id
        for lam in range(grid.num_slots):
            if (occ >> lam) & 1:
                continue   # slot lit -> no WLE
            for a, b in ((u, v), (v, u)):
                g.add_edge((WL, a, lam), (WL, b, lam),
                           kind="WLE", oms_id=oms.id, lam=lam, weight=_W_WLE)
                # TxE / RxE tie the wavelength layer to access at each endpoint
                g.add_edge((ACCESS, a), (WL, a, lam), kind="TxE", lam=lam, weight=_W_NEW_LP)
                g.add_edge((WL, b, lam), (ACCESS, b), kind="RxE", lam=lam, weight=_W_RXE)
    return g


def lpe_edges(g: nx.DiGraph) -> List[Tuple]:
    """All LPE edges as (u, v, data)."""
    return [(u, v, d) for u, v, d in g.edges(data=True) if d.get("kind") == "LPE"]


def wle_count_on_layer(g: nx.DiGraph, oms_id: str, lam: int) -> int:
    """Number of WLE edges for an OMS on a given wavelength layer (0 or 2:
    one per direction)."""
    return sum(1 for _, _, d in g.edges(data=True)
               if d.get("kind") == "WLE" and d.get("oms_id") == oms_id and d.get("lam") == lam)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_multilayer_graph.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/multilayer_graph.py tests/model/test_multilayer_graph.py
git commit -m "feat(graph): layered IP+optical auxiliary graph (LPE/WLE/TxE/RxE)"
```

---

## Task 3: `place_demands` (IGABAG k-best placement)

**Files:**
- Modify: `src/multilayer_optical_mcp/model/multilayer_graph.py`
- Test: `tests/model/test_multilayer_graph.py`

Walk `shortest_simple_paths` `access(src) → access(dst)` on a policy graph (`"groom_only"` drops TxE; `"new_only"` drops LPE; `"groom_or_new"` keeps both), collecting up to `k` distinct placements. Parse each path into reused-lightpath ids (LPE) and new-lightpath runs (TxE→WLEs(one λ)→RxE) — a path may have both (a hybrid). Realize each new run with one QoT call (reuse `allocation._build_loading` / `allocation._best_feasible_mode`); `restored_gbps` = min(demand, grooming bottleneck residual, new-lightpath mode rate).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/model/test_multilayer_graph.py
from multilayer_optical_mcp.model.assets import (
    FiberType as _FT, Fiber as _F, Amplifier as _A, OMS as _O, Lightpath as _L,
    Router as _R, IPLink as _I, Service,
)
from multilayer_optical_mcp.model.multilayer_graph import place_demands
from multilayer_optical_mcp.model.qot import QoTState


class FakeQot:
    def __init__(self, gsnr): self._g = gsnr
    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        return QoTState(gsnr_db=self._g, osnr_db=30.0, margin_db=0.0)


def test_groom_only_reuses_existing_lightpath():
    n = _one_lightpath_model()
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="A", dst="B",
                        demand_gbps=40.0, policy="groom_only")
    assert res, "expected at least one placement"
    assert res[0].reused_lightpaths == ("lp-AB",)
    assert res[0].new_lightpaths == ()
    assert res[0].restored_gbps == 40.0          # fits in 100G residual
    assert res[0].shortfall_gbps == 0.0


def test_groom_only_degrades_to_bottleneck_residual():
    n = _one_lightpath_model()
    # load 70G onto the IP link via a background service so residual is 30G < demand
    n.add_service(Service("s-load", "R1", "R2", 70.0, working_path=("ip-AB",)))
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="A", dst="B",
                        demand_gbps=40.0, policy="groom_only")
    assert res[0].restored_gbps == 30.0
    assert res[0].shortfall_gbps == 10.0


def test_new_only_lights_new_lightpath_when_no_existing_path():
    n = _one_lightpath_model()
    # Demand B->A: no existing lightpath that direction, must light a new one.
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="B", dst="A",
                        demand_gbps=100.0, policy="new_only")
    assert res
    assert res[0].reused_lightpaths == ()
    assert len(res[0].new_lightpaths) == 1
    assert res[0].new_lightpaths[0].oms_sequence == ("oms-AB",)
    assert res[0].restored_gbps == 100.0


def test_groom_only_empty_when_no_existing_path():
    n = _one_lightpath_model()
    g = build_layered_graph(n)
    assert place_demands(n, g, FakeQot(15.0), src="B", dst="A",
                         demand_gbps=10.0, policy="groom_only") == []


def _groom_plus_gap_model() -> NetworkModel:
    """A->M has an existing lightpath (lp-AM); M->B has free spectrum but NO
    existing lightpath. Demand A->B must groom A->M then light a new M->B
    lightpath -> a hybrid placement."""
    n = NetworkModel(modes=ModeRegistry([
        _TM_helper()]))
    n.register_fiber_type(_FT("SSMF", 0.2))
    for a in ("m1", "m2", "n1", "n2"):
        n.add_amplifier(_A(a, "advanced_toy", 20.0, 5.5))
    n.add_fiber(_F("fAM", "m1", "m2", 60.0, "SSMF"))
    n.add_fiber(_F("fMB", "n1", "n2", 60.0, "SSMF"))
    n.add_oms(_O("oms-AM", "A", "M", ("m1", "fAM", "m2")))
    n.add_oms(_O("oms-MB", "M", "B", ("n1", "fMB", "n2")))
    n.add_lightpath(_L("lp-AM", ("oms-AM",), "100G", 193.4e12))
    n.set_qot_state("lp-AM", QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=3.0))
    n.add_router(_R("RA", "A"))
    n.add_router(_R("RM", "M"))
    n.add_ip_link(_I("ip-AM", "RA", "RM", "lp-AM"))
    return n


def _TM_helper():
    from multilayer_optical_mcp.model.assets import TransceiverMode
    return TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                           symbol_rate_baud=32e9, channel_spacing_hz=100e9)


def test_groom_or_new_finds_hybrid_groom_plus_new():
    n = _groom_plus_gap_model()
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="A", dst="B",
                        demand_gbps=100.0, policy="groom_or_new")
    hybrids = [p for p in res if p.reused_lightpaths and p.new_lightpaths]
    assert hybrids, "expected a hybrid (groom A->M + new M->B)"
    h = hybrids[0]
    assert h.reused_lightpaths == ("lp-AM",)
    assert h.new_lightpaths[0].oms_sequence == ("oms-MB",)
    assert h.restored_gbps == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_multilayer_graph.py -v`
Expected: FAIL with `ImportError: cannot import name 'place_demands'`.

- [ ] **Step 3: Implement `place_demands`**

Append to `src/multilayer_optical_mcp/model/multilayer_graph.py`:

```python
from dataclasses import dataclass
from .spectrum import build_spectrum_state as _bss


@dataclass(frozen=True)
class NewLightpathRun:
    oms_sequence: Tuple[str, ...]
    lam: int
    mode_id: str
    gsnr_db: float
    bitrate_gbps: float


@dataclass(frozen=True)
class Placement:
    reused_lightpaths: Tuple[str, ...]
    new_lightpaths: Tuple[NewLightpathRun, ...]
    restored_gbps: float
    shortfall_gbps: float


# Enumeration budget: walk up to _PATH_BUDGET simple paths per policy, keeping
# up to _DEFAULT_K distinct feasible placements (the cost-ordered frontier).
_PATH_BUDGET = 64
_DEFAULT_K = 8


def _policy_graph(g: nx.DiGraph, policy: str) -> nx.DiGraph:
    """Restrict the graph to a lever:
      groom_only   - drop TxE edges: reuse existing lightpaths only (no new).
      new_only     - drop LPE edges: force fresh lightpaths (no reuse).
      groom_or_new - full graph (grooming wins on weight when feasible)."""
    if policy == "groom_or_new":
        return g
    if policy not in ("groom_only", "new_only"):
        raise ValueError(f"unknown policy {policy!r}")
    drop_kind = "TxE" if policy == "groom_only" else "LPE"
    h = g.copy()
    h.remove_edges_from([(u, v) for u, v, d in h.edges(data=True)
                         if d.get("kind") == drop_kind])
    return h


def _parse_path(g: nx.DiGraph, path: List) -> Tuple[List[str], List[Tuple[Tuple[str, ...], int]]]:
    """Split an access->access vertex path into (reused_lightpath_ids,
    new_runs) where each new_run is (oms_sequence, lam)."""
    reused: List[str] = []
    new_runs: List[Tuple[Tuple[str, ...], int]] = []
    cur_oms: List[str] = []
    cur_lam = None
    for a, b in zip(path, path[1:]):
        d = g.get_edge_data(a, b)
        kind = d.get("kind")
        if kind == "LPE":
            reused.append(d["lightpath_id"])
        elif kind == "WLE":
            cur_oms.append(d["oms_id"])
            cur_lam = d["lam"]
        elif kind == "RxE":
            if cur_oms:
                new_runs.append((tuple(cur_oms), cur_lam))
                cur_oms, cur_lam = [], None
        # TxE: entry into a wl layer; nothing to record
    return reused, new_runs


def _bottleneck_residual(g: nx.DiGraph, reused: List[str]) -> float:
    """Min residual_gbps across the reused LPE edges (inf if none reused)."""
    if not reused:
        return float("inf")
    by_lp = {d["lightpath_id"]: d["residual_gbps"]
             for _, _, d in g.edges(data=True) if d.get("kind") == "LPE"}
    return min(by_lp[lp] for lp in reused)


def place_demands(
    model: NetworkModel, g: nx.DiGraph, qot, *,
    src: str, dst: str, demand_gbps: float, policy: str,
    k: int = _DEFAULT_K, grid: SpectrumGrid | None = None,
) -> List["Placement"]:
    """IGABAG for one demand, returning up to `k` DISTINCT feasible placements
    (the cost-ordered frontier under the policy), each possibly degraded. A
    placement may reuse existing lightpaths (LPE), light new ones (TxE->WLEs->
    RxE), or BOTH (a hybrid). Empty list when no feasible path exists."""
    from .allocation import _build_loading, _best_feasible_mode  # reuse QoT seam
    grid = grid or SpectrumGrid.default()
    h = _policy_graph(g, policy)
    s, t = (ACCESS, src), (ACCESS, dst)
    if s not in h or t not in h or not nx.has_path(h, s, t):
        return []
    spectrum = _bss(model, grid)
    ref_mode = model.modes.list()[0].id
    out: List[Placement] = []
    seen: set = set()
    for i, path in enumerate(nx.shortest_simple_paths(h, s, t, weight="weight")):
        if i >= _PATH_BUDGET or len(out) >= k:
            break
        reused, new_runs = _parse_path(g, path)
        key = (tuple(reused), tuple(new_runs))   # new_runs carry (oms_seq, lam)
        if key in seen:
            continue
        seen.add(key)                            # evaluate each candidate once
        realized: List[NewLightpathRun] = []
        feasible = True
        new_cap = float("inf")
        for oms_seq, lam in new_runs:
            loading = _build_loading(grid, spectrum, oms_seq, lam, ref_mode)
            mode, gsnr = _best_feasible_mode(model, qot, oms_seq, loading, ref_mode)
            if mode is None:                     # margin < 0 -> infeasible run
                feasible = False
                break
            realized.append(NewLightpathRun(oms_seq, lam, mode.id, gsnr, mode.bitrate_gbps))
            new_cap = min(new_cap, mode.bitrate_gbps)
        if not feasible:
            continue
        groom_cap = _bottleneck_residual(g, reused)
        restored = min(demand_gbps, groom_cap, new_cap)
        if restored <= 0.0:
            continue
        out.append(Placement(
            reused_lightpaths=tuple(reused),
            new_lightpaths=tuple(realized),
            restored_gbps=restored,
            shortfall_gbps=max(0.0, demand_gbps - restored),
        ))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_multilayer_graph.py -v`
Expected: PASS (all graph + place_demands tests, including the hybrid).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/multilayer_graph.py tests/model/test_multilayer_graph.py
git commit -m "feat(graph): place_demands IGABAG k-best placement (groom/new/hybrid)"
```

---

## Task 4: `compute_restoration`

**Files:**
- Create: `src/multilayer_optical_mcp/model/restoration.py`
- Test: `tests/model/test_restoration.py`

Per-service, read-only. Prune by `avoid`, harvest k candidates via `place_demands` over `groom_or_new` + `new_only`, classify by lever (incl. `hybrid`), emit typed candidates, set status.

- [ ] **Step 1: Write the failing test**

```python
# tests/model/test_restoration.py
"""compute_restoration: per-service recovery over survivors. Read-only; emits
typed candidates (full + degraded); status solution/partial/no_solution."""
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, Router, IPLink, Service,
    TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.restoration import compute_restoration


class FakeQot:
    def __init__(self, gsnr=15.0): self._g = gsnr
    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        return QoTState(gsnr_db=self._g, osnr_db=30.0, margin_db=0.0)


def _diamond() -> NetworkModel:
    """A->B direct working lightpath (lp-direct), plus a survivor detour
    A->M->B with existing lightpaths lp-AM, lp-MB. Service rides lp-direct."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=100e9)]))
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("d1", "d2", "m1", "m2", "n1", "n2"):
        n.add_amplifier(Amplifier(a, "advanced_toy", 20.0, 5.5))
    n.add_fiber(Fiber("fAB", "d1", "d2", 80.0, "SSMF"))
    n.add_fiber(Fiber("fAM", "m1", "m2", 60.0, "SSMF"))
    n.add_fiber(Fiber("fMB", "n1", "n2", 60.0, "SSMF"))
    n.add_oms(OMS("oms-AB", "A", "B", ("d1", "fAB", "d2")))
    n.add_oms(OMS("oms-AM", "A", "M", ("m1", "fAM", "m2")))
    n.add_oms(OMS("oms-MB", "M", "B", ("n1", "fMB", "n2")))
    n.add_lightpath(Lightpath("lp-direct", ("oms-AB",), "100G", 193.4e12))
    n.add_lightpath(Lightpath("lp-AM", ("oms-AM",), "100G", 193.4e12))
    n.add_lightpath(Lightpath("lp-MB", ("oms-MB",), "100G", 193.4e12))
    for lp in ("lp-direct", "lp-AM", "lp-MB"):
        n.set_qot_state(lp, QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=3.0))
    n.add_router(Router("RA", "A"))
    n.add_router(Router("RM", "M"))
    n.add_router(Router("RB", "B"))
    n.add_ip_link(IPLink("ip-direct", "RA", "RB", "lp-direct"))
    n.add_ip_link(IPLink("ip-AM", "RA", "RM", "lp-AM"))
    n.add_ip_link(IPLink("ip-MB", "RM", "RB", "lp-MB"))
    n.add_service(Service("svc", "RA", "RB", 50.0,
                          working_path=("ip-direct",), protection_path=()))
    return n


def test_restoration_grooms_over_survivor_detour():
    n = _diamond()
    # fAB failed -> lp-direct down; survivors lp-AM + lp-MB groom A->M->B
    res = compute_restoration(n, FakeQot(), "svc", avoid={"assets": ["fAB"]})
    assert res.status is SolverStatus.SOLUTION
    groom = [c for c in res.candidates if c.lever == "ip_reroute"]
    assert groom and groom[0].reused_lightpaths == ("lp-AM", "lp-MB")
    assert groom[0].restored_gbps == 50.0
    assert groom[0].shortfall_gbps == 0.0


def test_restoration_no_survivor_is_no_solution():
    n = _diamond()
    # fail every fiber on both detour legs and the direct -> nothing survives
    res = compute_restoration(n, FakeQot(), "svc",
                              avoid={"assets": ["fAB", "fAM", "fMB"]})
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.candidates == ()


def test_restoration_degraded_when_survivor_partially_loaded():
    n = _diamond()
    # preload 80G on the M->B survivor leg so its residual is 20G
    n.add_service(Service("bg", "RM", "RB", 80.0, working_path=("ip-MB",)))
    # FakeQot below the 12 dB mode threshold -> the new-lightpath lever is
    # infeasible, isolating the degraded groom so status is PARTIAL (not a full
    # optical recovery). Grooming reuses existing lightpaths and calls no QoT.
    res = compute_restoration(n, FakeQot(10.0), "svc", avoid={"assets": ["fAB"]})
    assert res.status is SolverStatus.PARTIAL
    assert all(c.lever == "ip_reroute" for c in res.candidates)
    groom = res.candidates[0]
    assert groom.restored_gbps == 20.0
    assert groom.shortfall_gbps == 30.0


def _diamond_gap() -> NetworkModel:
    """Like _diamond but with NO existing lightpath on the M->B leg: recovery
    after fAB fails must groom A->M (lp-AM) AND light a new M->B lightpath."""
    m = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=100e9)]))
    m.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("d1", "d2", "m1", "m2", "n1", "n2"):
        m.add_amplifier(Amplifier(a, "advanced_toy", 20.0, 5.5))
    m.add_fiber(Fiber("fAB", "d1", "d2", 80.0, "SSMF"))
    m.add_fiber(Fiber("fAM", "m1", "m2", 60.0, "SSMF"))
    m.add_fiber(Fiber("fMB", "n1", "n2", 60.0, "SSMF"))
    m.add_oms(OMS("oms-AB", "A", "B", ("d1", "fAB", "d2")))
    m.add_oms(OMS("oms-AM", "A", "M", ("m1", "fAM", "m2")))
    m.add_oms(OMS("oms-MB", "M", "B", ("n1", "fMB", "n2")))
    m.add_lightpath(Lightpath("lp-direct", ("oms-AB",), "100G", 193.4e12))
    m.add_lightpath(Lightpath("lp-AM", ("oms-AM",), "100G", 193.4e12))
    for lp in ("lp-direct", "lp-AM"):
        m.set_qot_state(lp, QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=3.0))
    m.add_router(Router("RA", "A"))
    m.add_router(Router("RM", "M"))
    m.add_router(Router("RB", "B"))
    m.add_ip_link(IPLink("ip-direct", "RA", "RB", "lp-direct"))
    m.add_ip_link(IPLink("ip-AM", "RA", "RM", "lp-AM"))
    m.add_service(Service("svc", "RA", "RB", 50.0,
                          working_path=("ip-direct",), protection_path=()))
    return m


def test_restoration_hybrid_groom_plus_new_lightpath():
    n = _diamond_gap()
    res = compute_restoration(n, FakeQot(15.0), "svc", avoid={"assets": ["fAB"]})
    assert res.status is SolverStatus.SOLUTION
    hyb = [c for c in res.candidates if c.lever == "hybrid"]
    assert hyb, "expected a hybrid groom+new candidate"
    assert hyb[0].reused_lightpaths == ("lp-AM",)
    assert hyb[0].new_lightpaths[0].oms_sequence == ("oms-MB",)
    assert hyb[0].restored_gbps == 50.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_restoration.py -v`
Expected: FAIL with `ModuleNotFoundError: restoration`.

- [ ] **Step 3: Implement `compute_restoration`**

```python
# src/multilayer_optical_mcp/model/restoration.py
"""Per-service restoration: enumerate recovery candidates over survivors.

Read-only. Prunes the layered graph by an avoid-set (failed assets / risk
groups), harvests k-best placements over the groom_or_new frontier plus a
new_only fallback, and returns typed candidates (lever ip_reroute / optical_reroute
/ hybrid) with restored/shortfall capacity. Execution (validate_plan/commit_plan/
provision_lightpath) is Phase 7; this tool only enumerates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

from .network import NetworkModel
from .solvers import SolverStatus
from .multilayer_graph import build_layered_graph, place_demands, NewLightpathRun, Placement


@dataclass(frozen=True)
class RestorationCandidate:
    lever: str                              # "ip_reroute" | "optical_reroute" | "hybrid"
    reused_lightpaths: Tuple[str, ...]
    new_lightpaths: Tuple[NewLightpathRun, ...]
    restored_gbps: float
    shortfall_gbps: float
    cost_facets: Dict[str, float]           # transponders, new_lightpaths, hops


@dataclass(frozen=True)
class RestorationResult:
    status: SolverStatus
    service_id: str
    demand_gbps: float
    candidates: Tuple[RestorationCandidate, ...]


def _forbidden_assets(model: NetworkModel, avoid: Optional[dict]) -> FrozenSet[str]:
    """Physical asset ids to prune from the graph: the avoid-set's explicit
    assets plus the members of any named SRLG / risk group. NOTE: do not expand
    to endpoint nodes — a failed fiber must not condemn its healthy end ROADMs
    (which would prune parallel survivor OMS sharing those nodes)."""
    avoid = avoid or {}
    bad = set(avoid.get("assets", ()))
    avoid_rgs = set(avoid.get("risk_groups", ()))
    if avoid_rgs:
        for g in list(model.list_srlgs()) + list(model.list_risk_groups()):
            if g.id in avoid_rgs:
                bad.update(g.asset_ids)
    return frozenset(bad)


def _lever(p: Placement) -> str:
    if p.new_lightpaths and p.reused_lightpaths:
        return "hybrid"
    if p.new_lightpaths:
        return "optical_reroute"
    return "ip_reroute"


def _candidate(model: NetworkModel, p: Placement) -> RestorationCandidate:
    lever = _lever(p)
    cost = {
        "transponders": 2.0 * len(p.new_lightpaths),
        "new_lightpaths": float(len(p.new_lightpaths)),
        "hops": float(len(p.reused_lightpaths) + len(p.new_lightpaths)),
    }
    return RestorationCandidate(
        lever=lever,
        reused_lightpaths=p.reused_lightpaths,
        new_lightpaths=p.new_lightpaths,
        restored_gbps=p.restored_gbps,
        shortfall_gbps=p.shortfall_gbps,
        cost_facets=cost,
    )


def compute_restoration(
    model: NetworkModel, qot, service_id: str, avoid: Optional[dict] = None,
) -> RestorationResult:
    """Enumerate recovery candidates for a service over survivors. `avoid` is
    `{assets?: [...], risk_groups?: [...]}` (typically inject_failure's set)."""
    svc = model.get_service(service_id)
    src = model.get_router(svc.src_router).site
    dst = model.get_router(svc.dst_router).site
    forbidden = _forbidden_assets(model, avoid)
    g = build_layered_graph(model, forbidden_assets=forbidden)

    # groom_or_new harvests the cost-ordered frontier (groom + hybrid + cheap new);
    # new_only guarantees the pure-optical fallback even when many groom variants
    # would otherwise starve the budget. Dedup across both buckets.
    candidates: List[RestorationCandidate] = []
    seen: set = set()
    for policy in ("groom_or_new", "new_only"):
        for p in place_demands(model, g, qot, src=src, dst=dst,
                               demand_gbps=svc.demand_gbps, policy=policy):
            key = (p.reused_lightpaths,
                   tuple((r.oms_sequence, r.lam) for r in p.new_lightpaths))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(_candidate(model, p))

    candidates.sort(key=lambda c: (c.shortfall_gbps, c.cost_facets["transponders"],
                                   c.cost_facets["hops"]))
    if not candidates:
        status = SolverStatus.NO_SOLUTION
    elif any(c.shortfall_gbps == 0.0 for c in candidates):
        status = SolverStatus.SOLUTION
    else:
        status = SolverStatus.PARTIAL
    return RestorationResult(status, service_id, svc.demand_gbps, tuple(candidates))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_restoration.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/restoration.py tests/model/test_restoration.py
git commit -m "feat(restoration): per-service compute_restoration over the survivor graph"
```

---

## Task 5: View serializer + MCP tool

**Files:**
- Modify: `src/multilayer_optical_mcp/model/views.py`
- Modify: `src/multilayer_optical_mcp/server.py`
- Test: `tests/test_server_phase8.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_phase8.py
"""compute_restoration MCP tool returns a structured candidate list."""
from multilayer_optical_mcp.model.restoration import (
    RestorationResult, RestorationCandidate,
)
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.multilayer_graph import NewLightpathRun
from multilayer_optical_mcp.model.views import restoration_result_dict


def test_restoration_result_dict_shape():
    res = RestorationResult(
        status=SolverStatus.PARTIAL, service_id="svc", demand_gbps=50.0,
        candidates=(
            RestorationCandidate(
                lever="ip_reroute", reused_lightpaths=("lp-AM", "lp-MB"),
                new_lightpaths=(), restored_gbps=20.0, shortfall_gbps=30.0,
                cost_facets={"transponders": 0.0, "new_lightpaths": 0.0, "hops": 2.0}),
            RestorationCandidate(
                lever="optical_reroute", reused_lightpaths=(),
                new_lightpaths=(NewLightpathRun(("oms-AB",), 0, "100G", 15.0, 100.0),),
                restored_gbps=50.0, shortfall_gbps=0.0,
                cost_facets={"transponders": 2.0, "new_lightpaths": 1.0, "hops": 1.0}),
        ),
    )
    d = restoration_result_dict(res)
    assert d["status"] == "partial"
    assert d["service_id"] == "svc"
    assert len(d["candidates"]) == 2
    c0 = d["candidates"][0]
    assert c0["lever"] == "ip_reroute"
    assert c0["reused_lightpaths"] == ["lp-AM", "lp-MB"]
    assert c0["restored_gbps"] == 20.0
    c1 = d["candidates"][1]
    assert c1["new_lightpaths"][0]["oms_sequence"] == ["oms-AB"]
    assert c1["new_lightpaths"][0]["lam"] == 0
    assert c1["new_lightpaths"][0]["bitrate_gbps"] == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n multilayer-optical-mcp pytest tests/test_server_phase8.py -v`
Expected: FAIL with `ImportError: cannot import name 'restoration_result_dict'`.

- [ ] **Step 3: Implement the serializer**

Append to `src/multilayer_optical_mcp/model/views.py`:

```python
def restoration_result_dict(res) -> Dict[str, Any]:
    """Serialize a RestorationResult to a JSON-safe dict."""
    def _new_lp(r) -> dict:
        return {"oms_sequence": list(r.oms_sequence), "lam": r.lam,
                "mode_id": r.mode_id, "gsnr_db": r.gsnr_db,
                "bitrate_gbps": r.bitrate_gbps}

    def _cand(c) -> dict:
        return {"lever": c.lever,
                "reused_lightpaths": list(c.reused_lightpaths),
                "new_lightpaths": [_new_lp(r) for r in c.new_lightpaths],
                "restored_gbps": c.restored_gbps,
                "shortfall_gbps": c.shortfall_gbps,
                "cost_facets": dict(c.cost_facets)}

    return {"status": res.status.value,
            "service_id": res.service_id,
            "demand_gbps": res.demand_gbps,
            "candidates": [_cand(c) for c in res.candidates]}
```

- [ ] **Step 4: Wire the MCP tool**

In `src/multilayer_optical_mcp/server.py`, add to the imports (mirror the existing `_solve_allocation` / `make_adapter_evaluator` import block):

```python
from .model.restoration import compute_restoration as _compute_restoration
from .model.views import restoration_result_dict
```

Add the tool next to `inject_failure` (before `return app`):

```python
    @app.tool()
    def compute_restoration(service_id: str, avoid: dict | None = None) -> dict:
        """Enumerate recovery candidates for a service over survivors. `avoid` is
        {assets?: [...], risk_groups?: [...]} (typically a failure's asset set).
        Read-only: returns typed candidates (full + degraded) with status
        solution/partial/no_solution. Does not mutate or commit anything."""
        model = snapshots.current()
        qot = make_adapter_evaluator(model, results)
        res = _compute_restoration(model, qot, service_id, avoid=avoid)
        return restoration_result_dict(res)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/test_server_phase8.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `conda run -n multilayer-optical-mcp pytest -q`
Expected: PASS — all prior tests plus the new ones; nothing regressed.

- [ ] **Step 7: Commit**

```bash
git add src/multilayer_optical_mcp/model/views.py src/multilayer_optical_mcp/server.py tests/test_server_phase8.py
git commit -m "feat(server): expose compute_restoration tool + result serializer"
```

---

## Self-Review notes (resolved)

- **Spec coverage:** avoidance (Task 1) ✓; layered graph per-wavelength/no-conversion with WLE-from-bitmask and margin-gated LPE (Task 2) ✓; weights-not-capacity (moderate new-LP weight) + QoT-realized new lightpaths + degraded restored_gbps + k-best `place_demands` (Task 3) ✓; per-service enumeration over `groom_or_new` (groom/hybrid/cheap-new) + `new_only` guarantee, three-lever classification, typed status (Task 4) ✓; read-only tool + serializer (Task 5) ✓. Deferred items (multi-segment 3R regen, solve_allocation refactor, evaluate_objective, execution) are intentionally absent.
- **Hybrids:** classified by `_lever` when a placement has both `reused_lightpaths` and `new_lightpaths`; covered by `test_groom_or_new_finds_hybrid_groom_plus_new` (Task 3) and `test_restoration_hybrid_groom_plus_new_lightpath` (Task 4).
- **Type consistency:** `Placement`/`NewLightpathRun` (Task 3) are consumed unchanged by `compute_restoration` (Task 4) and `restoration_result_dict` (Task 5). `place_demands` (plural, returns `List[Placement]`) is the name used in Tasks 3–4. `build_layered_graph` signature (`forbidden_assets=`) is identical across Tasks 2–4. `restoration._forbidden_assets` depends only on `model.list_srlgs()/list_risk_groups()` (no solver imports).
- **Budget bound (documented, accepted):** k-shortest within a policy is a heuristic; in a pathologically dense survivor graph some mid-frontier candidates beyond rank K aren't enumerated. `new_only` guarantees the pure-optical fallback; per CLAUDE.md, a heuristic returning a feasible (not exhaustive) frontier is the contract.
- **Known modeling choice:** `Router.site` == optical-node id (documented in the header) is the `src_router → optical node` resolution flagged in the design.
```
