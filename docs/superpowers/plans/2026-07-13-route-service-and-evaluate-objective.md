# `route_service` + `evaluate_objective` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Canonical location:** On approval, save this document verbatim to
> `docs/superpowers/plans/2026-07-13-route-service-and-evaluate-objective.md`
> and commit it (plan-mode wrote it under `~/.claude/plans/`). All paths below are
> repo-relative to the project root.

**Goal:** Unify first-time routing and restoration at the service level on the layered IP+optical graph, add disjoint-pair routing to that graph, and implement `evaluate_objective` as the real 7-term cost vector that ranks candidates — retiring the proxy `cost_facets`.

**Architecture:** Two new modules (`model/multilayer_disjoint.py`, `model/objective.py`) plus one thin orchestrator (`model/route_service.py`) over the existing `place_demands` engine. Disjointness flattens a `Placement` to its physical OMS footprint and reuses `exposure.path_basis_keys`. Ranking clones the model, replays a candidate through the real `apply_op` machinery (seeding QoT from the run's already-computed `gsnr_db`), and scores the materialized state — so scoring and real commit cannot drift. `compute_restoration` becomes a thin wrapper; `solve_allocation` is rebased onto the same engine as a consuming packer.

**Tech Stack:** Python ≥3.11, NetworkX, `dataclasses`, pytest ≥8. FastMCP tool surface. GNPy stays behind the existing adapter — **no new GNPy calls** are introduced (QoT for new lightpaths is reused from the run).

## Global Constraints

- **Read vs. mutate strictly separated.** `route_service` and `evaluate_objective` are read-only / no-consume; all scoring happens on `model.clone()` throwaways. Ground truth is never mutated.
- **No solving in the LLM; typed outcomes never exceptions.** Every result carries a `SolverStatus` (`solution`/`partial`/`no_solution`); budget/infeasibility returns typed data, never raises.
- **Capacity = f(mode), margin-gated.** A new lightpath whose seeded margin < 0 must score capacity 0 (dropped / services_at_risk), never nominal line rate. This falls out of `network.ip_link_capacity_gbps` (network.py:338) only if QoT is seeded — see Task 3.
- **Disjointness on physical footprint, not lightpath identity.** Two different lightpaths sharing a fiber read as correlated. Flatten reused lightpaths to `oms_sequence` (Task 1).
- **Determinism.** Seeded/fixed inputs → identical menu order. No LLM in any test. QoT stubbed with an in-file `FakeQot` (existing convention, see below).
- **Env / test command:** `conda run -n multilayer-optical-mcp python -m pytest <path> -q`. `pythonpath=["src"]` is set in `pyproject.toml`, so no install needed.

---

## Context

Two graph substrates exist and don't share code: flat optical (`model/solvers.py`, used by `solve_rsa`/`solve_allocation`) and layered IP+optical (`model/multilayer_graph.py`, used by `restoration`). The layered graph has no disjoint-pair capability, `solve_allocation` is greenfield-only on the flat graph (a contract gap vs. CLAUDE.md), and `evaluate_objective` — the agent's ranking input for every disaster scenario (storm step 8, heat step 9) — does not exist. `restoration.py:62-69` already flags its `cost_facets` as proxies "until evaluate_objective's richer cost vector lands." This plan closes all three gaps at the service level on the layered graph, per the locked design in `docs/superpowers/specs/2026-07-13-route-service-and-evaluate-objective-design.md`.

### Verified facts the tasks depend on (do not re-derive)

- `place_demands(model, g, qot, *, src, dst, demand_gbps, policy, k=8, grid=None) -> List[Placement]` (`multilayer_graph.py:330`). Policies: `"groom_or_new"`, `"new_only"`, `"groom_only"`.
- `Placement` (`multilayer_graph.py:219`): `reused_lightpaths: Tuple[str,...]`, `new_lightpaths: Tuple[NewLightpathRun,...]`, `restored_gbps: float`, `shortfall_gbps: float`. Field is **`new_lightpaths`** (not `new_runs`).
- `NewLightpathRun` (`multilayer_graph.py:204`): `oms_sequence`, `lam: int`, `mode_id`, `gsnr_db`, `bitrate_gbps`, `src_node=""`, `dst_node=""`.
- `path_basis_keys(model, oms_sequence, *, basis, level) -> FrozenSet[str]` and `split_shared_keys(keys) -> (assets, groups)` (`exposure.py:130`, `:187`). `basis ∈ {physical, srlg, risk_group, union}`.
- `build_layered_graph(model, forbidden_assets=frozenset(), *, grid=None)` (`multilayer_graph.py:115`).
- `_forbidden_assets(model, avoid) -> FrozenSet[str]`, `_lever(p) -> str` (`restoration.py:38`, `:53`).
- `SolverStatus` (`solvers.py:42`, str-Enum): `SOLUTION`/`PARTIAL`/`NO_SOLUTION`, `.value` = lowercase.
- `apply_op(model, op)` (`plan.py:59`), ops `ProvisionLightpath(lightpath, ip_link=None)`, `RerouteService(service_id, ip_path)`, etc.; `Plan(ops)`.
- `NetworkModel.clone()` returns an **unfrozen** independent copy (`network.py:61`). `set_qot_state(lp_id, QoTState)` (`network.py:313`), `get_qot_state` raises `LookupError` if unrecorded. `ip_link_capacity_gbps(lid)` = mode bitrate, 0.0 when `margin<0`, `LookupError` when no QoT (`network.py:338`).
- `QoTState(gsnr_db, osnr_db, margin_db, limiting_element_id=None)` (`qot.py:6`). Mode required GSNR: `model.modes.get(mode_id).required_gsnr_db`.
- `simulate_ip_routing(model) -> IPRoutingResult` (`ip_routing.py:201`): `.utilizations` (each `LinkUtilization(ip_link_id, offered_gbps, capacity_gbps, utilization, down)`), `.congested_links`, `.down_links`, `.dropped_services` (each `DroppedService(service_id, reason, on_link)`), `.overflow_gbps`. **Dropped services contribute NO link load** (`active_load_per_link` skips paths that are `None`), so dropped-demand and overflow cover disjoint traffic (the S5-9 no-double-count basis).
- `build_spectrum_state(model, grid) -> Dict[str,int]` per-OMS slot bitmask (`spectrum.py:66`). `SpectrumGrid.default()`; `grid.freq(slot) -> hz` (`spectrum.py:41`).
- `margin_threshold_sweep(model, threshold_db) -> List[MarginSweepRow]` (`whatif.py:55`); row has `lightpath_id`, `margin_db`.
- `set_service_working_path(service_id, ip_path)` validates contiguity via `is_contiguous_path` (`network.py:243`, `ip_routing.py:262`) — so the IP path handed to `RerouteService` MUST be an ordered contiguous walk.
- `Router(id, site)`, `IPLink(id, a_router, z_router, lightpath_id)`, `Fiber(..., length_km)`. `get_fiber(id)`, `get_oms(id)` (`.elements` includes fiber uids).
- Tests build models in-code (no JSON fixtures for these layers) with an in-file `FakeQot`; serializers are `*_dict(res)` in `model/views.py`; tools are `@app.tool()` nested fns in `build_app()` (`server.py`), impls + serializers imported *inside* `build_app`. `make_adapter_evaluator(model, results)` builds the QoT evaluator solver tools pass down.

---

## File Structure

- **Create** `src/multilayer_optical_mcp/model/multilayer_disjoint.py` — `placement_footprint_keys`, `disjoint_pairs`, `PlacementPair`. One responsibility: disjointness over `Placement`s.
- **Create** `src/multilayer_optical_mcp/model/objective.py` — `ObjectiveResult`, `evaluate_objective`, plus candidate materialization (`apply_candidate`, `score_candidate`, `score_pair`) and the ordered-IP-path stitch.
- **Create** `src/multilayer_optical_mcp/model/route_service.py` — `route_service`, `RouteServiceResult`, `RouteServiceCandidate`, `RoutePair`.
- **Modify** `src/multilayer_optical_mcp/model/restoration.py` — `compute_restoration` becomes a wrapper; `cost_facets` → `cost_vector`.
- **Modify** `src/multilayer_optical_mcp/model/allocation.py` — rebase `solve_allocation` packer onto the layered engine + `disjoint_pairs`.
- **Modify** `src/multilayer_optical_mcp/model/network.py` — add `list_routers()` accessor (Task 3).
- **Modify** `src/multilayer_optical_mcp/model/views.py` — `objective_result_dict`, `route_service_result_dict`; rename in `restoration_result_dict`.
- **Modify** `src/multilayer_optical_mcp/server.py` — register `route_service`, `evaluate_objective`.
- **Create** tests under `tests/model/` and extend `tests/test_server_phase8.py`.

---

### Task 1: Disjoint-pair primitive over `Placement`s

**Files:**
- Create: `src/multilayer_optical_mcp/model/multilayer_disjoint.py`
- Test: `tests/model/test_multilayer_disjoint.py`

**Interfaces:**
- Consumes: `exposure.path_basis_keys`, `exposure.split_shared_keys`; `multilayer_graph.Placement`, `NewLightpathRun`.
- Produces:
  - `placement_footprint_keys(model, placement, *, basis, level) -> FrozenSet[str]`
  - `PlacementPair` dataclass: `working: Placement`, `protection: Placement`, `disjoint: bool`, `shared_assets: Tuple[str,...]`, `shared_groups: Tuple[str,...]`, `overlap: int`.
  - `disjoint_pairs(model, candidates, *, basis, level, best_effort, top_n) -> List[PlacementPair]`

- [ ] **Step 1: Write the failing test — footprint flatten (the correctness trap)**

```python
# tests/model/test_multilayer_disjoint.py
from multilayer_optical_mcp.model.multilayer_disjoint import (
    placement_footprint_keys, disjoint_pairs)
from multilayer_optical_mcp.model.multilayer_graph import Placement, NewLightpathRun
# _diamond() builds a 4-node model where oms 'omsAB' is shared by two lightpaths.
# Reuse the topology-builder style from tests/model/test_restoration.py.

def test_reused_and_new_sharing_a_fiber_read_as_correlated(diamond):
    model = diamond
    reused = Placement(reused_lightpaths=("lpAB",), new_lightpaths=(), restored_gbps=100.0, shortfall_gbps=0.0)
    fresh  = Placement(reused_lightpaths=(),
                       new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0, src_node="A", dst_node="B"),),
                       restored_gbps=100.0, shortfall_gbps=0.0)
    ka = placement_footprint_keys(model, reused, basis="physical", level="link")
    kb = placement_footprint_keys(model, fresh,  basis="physical", level="link")
    assert ka & kb        # they share omsAB's fiber -> correlated, NOT disjoint
```

- [ ] **Step 2: Run it — expect ImportError / fail**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_multilayer_disjoint.py -q`
Expected: FAIL (module/function not defined).

- [ ] **Step 3: Implement `placement_footprint_keys` + `disjoint_pairs`**

```python
# src/multilayer_optical_mcp/model/multilayer_disjoint.py
"""Disjointness over multilayer Placements.

A Placement's physical footprint is the union of the OMS its reused lightpaths
traverse AND the OMS its new runs light. Flattening reused lightpaths to their
oms_sequence (never treating a lightpath id as opaque) is the load-bearing step:
two different lightpaths sharing a fiber must read as correlated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .network import NetworkModel
from .multilayer_graph import Placement
from .exposure import path_basis_keys, split_shared_keys


def placement_footprint_keys(model: NetworkModel, placement: Placement, *,
                             basis: str, level: str) -> frozenset[str]:
    oms_seq: List[str] = []
    for lp_id in placement.reused_lightpaths:
        oms_seq += list(model.get_lightpath(lp_id).oms_sequence)
    for run in placement.new_lightpaths:
        oms_seq += list(run.oms_sequence)
    return path_basis_keys(model, tuple(oms_seq), basis=basis, level=level)


@dataclass(frozen=True)
class PlacementPair:
    working: Placement
    protection: Placement
    disjoint: bool
    shared_assets: Tuple[str, ...]
    shared_groups: Tuple[str, ...]
    overlap: int                 # count of shared namespaced keys (S6-8 semantics)


def disjoint_pairs(model: NetworkModel, candidates, *, basis: str, level: str,
                   best_effort: bool, top_n: int) -> List[PlacementPair]:
    """O(k^2) pairwise scan. Fully-disjoint pairs (shared == empty) first; if none
    and best_effort, min-overlap pairs (ranked by count of shared keys, S6-8 —
    count of namespaced keys, not physical severity). Returns up to top_n pairs."""
    keyed = [(c, placement_footprint_keys(model, c, basis=basis, level=level))
             for c in candidates]
    disjoint: List[PlacementPair] = []
    overlapping: List[PlacementPair] = []
    for i in range(len(keyed)):
        ci, ki = keyed[i]
        for j in range(i + 1, len(keyed)):
            cj, kj = keyed[j]
            shared = ki & kj
            if not shared:
                disjoint.append(PlacementPair(ci, cj, True, (), (), 0))
            else:
                assets, groups = split_shared_keys(shared)
                overlapping.append(
                    PlacementPair(ci, cj, False, assets, groups, len(shared)))
    if disjoint:
        return disjoint[:top_n]
    if best_effort and overlapping:
        overlapping.sort(key=lambda p: p.overlap)
        return overlapping[:top_n]
    return []
```

- [ ] **Step 4: Run test — expect PASS**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_multilayer_disjoint.py -q`
Expected: PASS.

- [ ] **Step 5: Add best_effort + parity tests, run, then commit**

```python
def test_full_disjoint_pair_found(diamond):
    # Two placements over vertex-disjoint OMS routes -> a disjoint pair exists.
    ...
    pairs = disjoint_pairs(model, [pA, pB], basis="physical", level="link",
                           best_effort=False, top_n=5)
    assert pairs and pairs[0].disjoint

def test_best_effort_returns_min_overlap_when_none_disjoint(diamond):
    pairs = disjoint_pairs(model, [pA, pB], basis="physical", level="link",
                           best_effort=True, top_n=5)
    assert pairs and not pairs[0].disjoint and pairs[0].overlap >= 1

def test_no_disjoint_no_best_effort_returns_empty(diamond):
    assert disjoint_pairs(model, [pA, pB], basis="physical", level="link",
                          best_effort=False, top_n=5) == []
```

Run the file; expect PASS. Then:
```bash
git add src/multilayer_optical_mcp/model/multilayer_disjoint.py tests/model/test_multilayer_disjoint.py
git commit -m "feat(disjoint): placement-footprint disjoint_pairs over the layered graph"
```

---

### Task 2: `evaluate_objective` state scorer

**Files:**
- Create: `src/multilayer_optical_mcp/model/objective.py`
- Test: `tests/model/test_objective.py`

**Interfaces:**
- Consumes: `ip_routing.simulate_ip_routing`, `spectrum.build_spectrum_state`, `spectrum.SpectrumGrid`, `whatif.margin_threshold_sweep`, `network.NetworkModel`.
- Produces:
  - `ObjectiveResult` dataclass (7 raw terms + `scalar`).
  - `evaluate_objective(model, weights=None, *, spare_transponders=None, at_risk_threshold_db=1.0) -> ObjectiveResult`

- [ ] **Step 1: Write the failing test**

```python
# tests/model/test_objective.py
from multilayer_optical_mcp.model.objective import evaluate_objective, ObjectiveResult

def test_objective_vector_on_seeded_state(loaded_model):
    r = evaluate_objective(loaded_model)
    assert isinstance(r, ObjectiveResult)
    assert r.transponders == 2.0 * len(loaded_model.list_lightpaths())
    assert r.max_util >= 0.0
    # default weights = 1.0, total_margin subtracted:
    assert r.scalar == (r.spectrum_used + r.transponders + r.max_util
                        + r.dropped_traffic + r.added_latency
                        + r.services_at_risk - r.total_margin)

def test_margin_negative_lightpath_scores_as_dropped_not_nominal(down_model):
    # a lightpath seeded margin<0 -> its IP link capacity 0 -> its service dropped
    r = evaluate_objective(down_model)
    assert r.dropped_traffic > 0.0
```

- [ ] **Step 2: Run — expect fail (module missing)**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_objective.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement `evaluate_objective`**

```python
# src/multilayer_optical_mcp/model/objective.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .network import NetworkModel
from .spectrum import SpectrumGrid, build_spectrum_state
from .ip_routing import simulate_ip_routing
from .whatif import margin_threshold_sweep

_PROP_MS_PER_KM = 0.005   # ~5 us/km one-way fiber propagation


@dataclass(frozen=True)
class ObjectiveResult:
    spectrum_used: int
    transponders: float
    max_util: float
    dropped_traffic: float
    added_latency: float
    total_margin: float
    services_at_risk: int
    scalar: float


def _oms_seq_length_km(model: NetworkModel, oms_sequence) -> float:
    total = 0.0
    for oms_id in oms_sequence:
        for el in model.get_oms(oms_id).elements:
            try:
                total += model.get_fiber(el).length_km
            except (KeyError, LookupError):
                continue          # non-fiber element (amp/roadm)
    return total


def _active_working_lightpaths(model, svc):
    """The lightpath ids under a service's working IP path (its declared intent)."""
    out = []
    for ip_id in svc.working_path:
        out.append(model.get_ip_link(ip_id).lightpath_id)
    return out


def evaluate_objective(model: NetworkModel, weights: Optional[Dict[str, float]] = None,
                       *, spare_transponders: Optional[int] = None,
                       at_risk_threshold_db: float = 1.0) -> ObjectiveResult:
    w = weights or {}
    grid = SpectrumGrid.default()

    spectrum_used = sum(bin(mask).count("1")
                        for mask in build_spectrum_state(model, grid).values())

    tp = 2.0 * len(model.list_lightpaths())
    if spare_transponders is not None:
        tp = max(0.0, tp - float(spare_transponders))

    ipr = simulate_ip_routing(model)
    max_util = max((u.utilization for u in ipr.utilizations
                    if u.utilization is not None), default=0.0)

    dropped_ids = {d.service_id for d in ipr.dropped_services}
    dropped_demand = sum(model.get_service(sid).demand_gbps for sid in dropped_ids)
    # Dropped services carry no link load (active_load skips path==None), so
    # dropped_demand and overflow_gbps cover disjoint traffic -> no double count.
    dropped_traffic = dropped_demand + ipr.overflow_gbps

    added_latency = 0.0
    for svc in model.list_services():
        if svc.id in dropped_ids:
            continue
        for lp_id in _active_working_lightpaths(model, svc):
            added_latency += _PROP_MS_PER_KM * _oms_seq_length_km(
                model, model.get_lightpath(lp_id).oms_sequence)

    total_margin = 0.0
    for lp in model.list_lightpaths():
        try:
            total_margin += model.get_qot_state(lp.id).margin_db
        except LookupError:
            continue

    at_risk_lps = {row.lightpath_id
                   for row in margin_threshold_sweep(model, at_risk_threshold_db)}
    services_at_risk = sum(
        1 for svc in model.list_services()
        if set(_active_working_lightpaths(model, svc)) & at_risk_lps)

    scalar = (w.get("spectrum_used", 1.0) * spectrum_used
              + w.get("transponders", 1.0) * tp
              + w.get("max_util", 1.0) * max_util
              + w.get("dropped_traffic", 1.0) * dropped_traffic
              + w.get("added_latency", 1.0) * added_latency
              - w.get("total_margin", 1.0) * total_margin
              + w.get("services_at_risk", 1.0) * services_at_risk)

    return ObjectiveResult(spectrum_used, tp, max_util, dropped_traffic,
                           added_latency, total_margin, services_at_risk, scalar)
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_objective.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/objective.py tests/model/test_objective.py
git commit -m "feat(objective): evaluate_objective 7-term cost vector + weighted scalar"
```

---

### Task 3: Candidate materialization (`apply_candidate`, `score_candidate`, `score_pair`)

**Files:**
- Modify: `src/multilayer_optical_mcp/model/network.py` (add `list_routers()`)
- Modify: `src/multilayer_optical_mcp/model/objective.py` (append materialization)
- Test: `tests/model/test_objective_scoring.py`

**Interfaces:**
- Consumes: `plan.apply_op`, `plan.ProvisionLightpath`, `plan.RerouteService`; `assets.Lightpath`, `assets.IPLink`; `qot.QoTState`; `SpectrumGrid.freq`; `Placement`.
- Produces (in `objective.py`):
  - `apply_candidate(work, placement, service) -> None` — mutates a clone in place (provision new runs + seed QoT + reroute working).
  - `score_candidate(model, placement, service, weights=None) -> ObjectiveResult`
  - `score_pair(model, working, protection, service, weights=None) -> ObjectiveResult`

- [ ] **Step 1: Add `list_routers()` accessor (network.py), write the failing scoring test**

`list_routers` mirrors `list_services` (network.py:302):
```python
    def list_routers(self):
        return tuple(self._routers.values())
```

```python
# tests/model/test_objective_scoring.py
from multilayer_optical_mcp.model.objective import score_candidate, evaluate_objective
from multilayer_optical_mcp.model.multilayer_graph import Placement, NewLightpathRun

def test_score_candidate_matches_real_commit(diamond_service):
    model, svc = diamond_service   # empty net + one service A->B, demand 100
    cand = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0, src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    scored = score_candidate(model, cand, svc)
    # Independently materialize the same candidate on a clone and score directly:
    work = model.clone()
    from multilayer_optical_mcp.model.objective import apply_candidate
    apply_candidate(work, cand, svc)
    assert scored == evaluate_objective(work)   # identical apply path -> identical numbers
```

- [ ] **Step 2: Run — expect fail**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_objective_scoring.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement materialization in `objective.py`**

```python
# append to src/multilayer_optical_mcp/model/objective.py
from .plan import apply_op, ProvisionLightpath, RerouteService
from .assets import Lightpath, IPLink
from .qot import QoTState


def _stitch_ip_path(segments, src_router, dst_router):
    """Order (a_router, z_router, ip_id) segments into a contiguous walk
    src_router -> dst_router. Each segment usable in either orientation."""
    remaining = list(segments)
    path = []
    node = src_router
    while node != dst_router and remaining:
        for k, (a, z, ip_id) in enumerate(remaining):
            if a == node:
                path.append(ip_id); node = z; remaining.pop(k); break
            if z == node:
                path.append(ip_id); node = a; remaining.pop(k); break
        else:
            break     # no segment continues the walk (should not happen for a real placement)
    return tuple(path)


def apply_candidate(work, placement, service, *, prefix="cand") -> None:
    """Materialize a Placement on `work` (a clone): provision each new run as a
    lightpath+IP link, SEED QoT from the run's gsnr_db (real provision does not
    seed QoT; real commit reaches the same numbers via a post-commit recompute),
    then reroute the service's working path onto the placement."""
    grid = SpectrumGrid.default()
    site_to_router = {r.site: r.id for r in work.list_routers()}
    lp_to_iplink = {l.lightpath_id: l for l in work.list_ip_links()}
    segments = []
    # reused legs: reuse their existing IP link binding
    for lp_id in placement.reused_lightpaths:
        link = lp_to_iplink[lp_id]
        segments.append((link.a_router, link.z_router, link.id))
    # new legs: provision lightpath + IP link, seed QoT
    for i, run in enumerate(placement.new_lightpaths):
        lp_id = f"lp-{prefix}-{service.id}-{i}"
        ipl_id = f"ipl-{prefix}-{service.id}-{i}"
        a = site_to_router[run.src_node]; z = site_to_router[run.dst_node]
        apply_op(work, ProvisionLightpath(
            lightpath=Lightpath(id=lp_id, oms_sequence=run.oms_sequence,
                                mode_id=run.mode_id, center_freq_hz=grid.freq(run.lam)),
            ip_link=IPLink(id=ipl_id, a_router=a, z_router=z, lightpath_id=lp_id)))
        req = work.modes.get(run.mode_id).required_gsnr_db
        work.set_qot_state(lp_id, QoTState(gsnr_db=run.gsnr_db, osnr_db=run.gsnr_db,
                                           margin_db=run.gsnr_db - req))
        segments.append((a, z, ipl_id))
    ip_path = _stitch_ip_path(segments, service.src_router, service.dst_router)
    apply_op(work, RerouteService(service_id=service.id, ip_path=ip_path))


def score_candidate(model, placement, service, weights=None) -> ObjectiveResult:
    work = model.clone()
    apply_candidate(work, placement, service)
    return evaluate_objective(work, weights)


def score_pair(model, working, protection, service, weights=None) -> ObjectiveResult:
    """Provision BOTH legs (protection's transponders/spectrum/total_margin count),
    route the working leg. Protection is 1:1 reserved and idle -> not loaded, so it
    contributes no IP load; its cost surfaces via provisioned lightpaths."""
    work = model.clone()
    apply_candidate(work, working, service, prefix="work")
    # provision protection's new lightpaths (no reroute) so their cost is counted
    grid = SpectrumGrid.default()
    site_to_router = {r.site: r.id for r in work.list_routers()}
    for i, run in enumerate(protection.new_lightpaths):
        lp_id = f"lp-prot-{service.id}-{i}"
        ipl_id = f"ipl-prot-{service.id}-{i}"
        a = site_to_router[run.src_node]; z = site_to_router[run.dst_node]
        apply_op(work, ProvisionLightpath(
            lightpath=Lightpath(id=lp_id, oms_sequence=run.oms_sequence,
                                mode_id=run.mode_id, center_freq_hz=grid.freq(run.lam)),
            ip_link=IPLink(id=ipl_id, a_router=a, z_router=z, lightpath_id=lp_id)))
        req = work.modes.get(run.mode_id).required_gsnr_db
        work.set_qot_state(lp_id, QoTState(gsnr_db=run.gsnr_db, osnr_db=run.gsnr_db,
                                           margin_db=run.gsnr_db - req))
    return evaluate_objective(work, weights)
```

- [ ] **Step 4: Run scoring test — expect PASS. Add a margin-gate test**

```python
def test_margin_negative_candidate_scores_dropped(diamond_service_lowgsnr):
    # run.gsnr_db below the mode's required_gsnr -> seeded margin<0 -> capacity 0
    model, svc = diamond_service_lowgsnr
    cand = Placement((), (NewLightpathRun(("omsAB",),0,"100G",1.0,100.0,src_node="A",dst_node="B"),), 0.0, 100.0)
    r = score_candidate(model, cand, svc)
    assert r.dropped_traffic > 0.0     # not nominal line rate
```

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_objective_scoring.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py src/multilayer_optical_mcp/model/objective.py tests/model/test_objective_scoring.py
git commit -m "feat(objective): candidate materialization via apply_op + run-seeded QoT"
```

---

### Task 4: `route_service` orchestrator

**Files:**
- Create: `src/multilayer_optical_mcp/model/route_service.py`
- Test: `tests/model/test_route_service.py`

**Interfaces:**
- Consumes: `build_layered_graph`, `place_demands`; `restoration._forbidden_assets`, `restoration._lever`; `multilayer_disjoint.disjoint_pairs`; `objective.score_candidate`, `objective.score_pair`; `solvers.SolverStatus`.
- Produces:
  - `RouteServiceCandidate`: `lever`, `reused_lightpaths`, `new_lightpaths`, `restored_gbps`, `shortfall_gbps`, `cost_vector: Dict[str,float]`.
  - `RoutePair`: `working: RouteServiceCandidate`, `protection: RouteServiceCandidate`, `disjoint: bool`, `shared_assets`, `shared_groups`, `cost_vector`.
  - `RouteServiceResult`: `status`, `service_id`, `demand_gbps`, `protected: bool`, `candidates: Tuple[RouteServiceCandidate,...]`, `pairs: Tuple[RoutePair,...]`.
  - `route_service(model, qot, service_id, *, protected=False, basis="physical", level="link", best_effort=False, avoid=None, weights=None, k=8, top_n=5) -> RouteServiceResult`

- [ ] **Step 1: Write failing tests (unprotected menu + protected menu)**

```python
# tests/model/test_route_service.py
from multilayer_optical_mcp.model.route_service import route_service
from multilayer_optical_mcp.model.solvers import SolverStatus

def test_unprotected_first_time_routing_menu(diamond_service):
    model, svc = diamond_service   # empty net
    res = route_service(model, FakeQot(), svc.id)     # avoid=None -> first-time
    assert res.status in (SolverStatus.SOLUTION, SolverStatus.PARTIAL)
    assert res.candidates                              # a menu of candidates
    assert all("cost_vector" in vars(c) or hasattr(c, "cost_vector") for c in res.candidates)
    # sorted ascending by scalar:
    assert res.candidates == tuple(sorted(res.candidates,
        key=lambda c: c.cost_vector["scalar"]))

def test_protected_returns_disjoint_pair_menu(diamond_service_two_routes):
    model, svc = diamond_service_two_routes
    res = route_service(model, FakeQot(), svc.id, protected=True, best_effort=False)
    assert res.protected and res.pairs and res.pairs[0].disjoint

def test_protected_best_effort_partial(diamond_service_shrunk):
    res = route_service(model, FakeQot(), svc.id, protected=True, best_effort=True)
    assert res.status is SolverStatus.PARTIAL and not res.pairs[0].disjoint
```

- [ ] **Step 2: Run — expect fail**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_route_service.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement `route_service`**

```python
# src/multilayer_optical_mcp/model/route_service.py
"""Service-level routing/restoration on the layered graph (menu, no-consume).

avoid=None -> first-time routing (empty net -> all-new candidates).
avoid={assets?,risk_groups?} -> restoration over survivors.
protected=False -> up to k single candidates; protected=True -> disjoint-pair menu.
Read-only: every score is computed on a throwaway clone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .network import NetworkModel
from .solvers import SolverStatus
from .multilayer_graph import build_layered_graph, place_demands, NewLightpathRun
from .restoration import _forbidden_assets, _lever
from .multilayer_disjoint import disjoint_pairs
from .objective import score_candidate, score_pair


@dataclass(frozen=True)
class RouteServiceCandidate:
    lever: str
    reused_lightpaths: Tuple[str, ...]
    new_lightpaths: Tuple[NewLightpathRun, ...]
    restored_gbps: float
    shortfall_gbps: float
    cost_vector: Dict[str, float]


@dataclass(frozen=True)
class RoutePair:
    working: RouteServiceCandidate
    protection: RouteServiceCandidate
    disjoint: bool
    shared_assets: Tuple[str, ...]
    shared_groups: Tuple[str, ...]
    cost_vector: Dict[str, float]


@dataclass(frozen=True)
class RouteServiceResult:
    status: SolverStatus
    service_id: str
    demand_gbps: float
    protected: bool
    candidates: Tuple[RouteServiceCandidate, ...] = ()
    pairs: Tuple[RoutePair, ...] = ()


def _vec(res) -> Dict[str, float]:
    d = {k: getattr(res, k) for k in
         ("spectrum_used", "transponders", "max_util", "dropped_traffic",
          "added_latency", "total_margin", "services_at_risk")}
    d["scalar"] = res.scalar
    return d


def _harvest(model, qot, g, src, dst, demand, k):
    """The restoration harvest: groom_or_new + new_only frontiers, deduped on the
    lambda-free route identity (matches restoration.compute_restoration)."""
    out = []
    seen = set()
    for policy in ("groom_or_new", "new_only"):
        for p in place_demands(model, g, qot, src=src, dst=dst,
                               demand_gbps=demand, policy=policy, k=k):
            key = (p.reused_lightpaths, tuple(r.oms_sequence for r in p.new_lightpaths))
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


def _status(shortfalls: List[float]) -> SolverStatus:
    if not shortfalls:
        return SolverStatus.NO_SOLUTION
    if any(s == 0.0 for s in shortfalls):
        return SolverStatus.SOLUTION
    return SolverStatus.PARTIAL


def route_service(model: NetworkModel, qot, service_id: str, *, protected: bool = False,
                  basis: str = "physical", level: str = "link", best_effort: bool = False,
                  avoid: Optional[dict] = None, weights: Optional[dict] = None,
                  k: int = 8, top_n: int = 5) -> RouteServiceResult:
    svc = model.get_service(service_id)
    src = model.get_router(svc.src_router).site
    dst = model.get_router(svc.dst_router).site
    g = build_layered_graph(model, forbidden_assets=_forbidden_assets(model, avoid))
    placements = _harvest(model, qot, g, src, dst, svc.demand_gbps, k)

    if not protected:
        cands = [RouteServiceCandidate(
                    _lever(p), p.reused_lightpaths, p.new_lightpaths,
                    p.restored_gbps, p.shortfall_gbps,
                    _vec(score_candidate(model, p, svc, weights)))
                 for p in placements]
        cands.sort(key=lambda c: c.cost_vector["scalar"])
        return RouteServiceResult(_status([c.shortfall_gbps for c in cands]),
                                  service_id, svc.demand_gbps, False,
                                  candidates=tuple(cands))

    pp = disjoint_pairs(model, placements, basis=basis, level=level,
                        best_effort=best_effort, top_n=top_n)
    pairs: List[RoutePair] = []
    for p in pp:
        sr = score_pair(model, p.working, p.protection, svc, weights)
        def _c(pl):
            return RouteServiceCandidate(_lever(pl), pl.reused_lightpaths,
                pl.new_lightpaths, pl.restored_gbps, pl.shortfall_gbps, {})
        pairs.append(RoutePair(_c(p.working), _c(p.protection), p.disjoint,
                               p.shared_assets, p.shared_groups, _vec(sr)))
    pairs.sort(key=lambda pr: pr.cost_vector["scalar"])
    if not pairs:
        status = SolverStatus.NO_SOLUTION
    elif pairs[0].disjoint:
        status = SolverStatus.SOLUTION
    else:
        status = SolverStatus.PARTIAL
    return RouteServiceResult(status, service_id, svc.demand_gbps, True,
                              pairs=tuple(pairs))
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_route_service.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/route_service.py tests/model/test_route_service.py
git commit -m "feat(route_service): unified service-level routing/restoration menu + protected pairs"
```

---

### Task 5: `compute_restoration` → wrapper; `cost_facets` → `cost_vector`

**Files:**
- Modify: `src/multilayer_optical_mcp/model/restoration.py`
- Modify: `src/multilayer_optical_mcp/model/views.py:251-269` (`restoration_result_dict`)
- Modify: `tests/test_server_phase8.py` (constructor kwargs + assertions)
- Test: `tests/model/test_restoration.py` (must still pass unchanged)

**Interfaces:**
- Consumes: `route_service.route_service`.
- Produces: `RestorationCandidate.cost_vector` (was `cost_facets`); `compute_restoration` unchanged signature `(model, qot, service_id, avoid=None) -> RestorationResult`, result shape preserved except the field rename.

- [ ] **Step 1: Update the back-compat test first (name the new contract)**

In `tests/test_server_phase8.py`, change the two `cost_facets={...}` constructor kwargs to `cost_vector={...}` and update the serialized-key assertion from `"cost_facets"` to `"cost_vector"`. Keep `tests/model/test_restoration.py` assertions on `.lever/.reused_lightpaths/.new_lightpaths/.restored_gbps/.shortfall_gbps` untouched (they must still pass).

- [ ] **Step 2: Run the restoration + phase8 tests — expect fail (field renamed, impl not yet)**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_restoration.py tests/test_server_phase8.py -q`
Expected: FAIL.

- [ ] **Step 3: Rename in `restoration.py` and rebuild `compute_restoration` as a wrapper**

Rename the `RestorationCandidate.cost_facets` field to `cost_vector`. Reimplement `compute_restoration` to delegate to `route_service(..., protected=False, avoid=avoid)` and map each `RouteServiceCandidate` back to a `RestorationCandidate` (carrying `cost_vector`). Preserve the existing sort intent by ordering on `(shortfall_gbps, cost_vector["scalar"])`:

```python
from .route_service import route_service

def compute_restoration(model, qot, service_id, avoid=None) -> RestorationResult:
    rs = route_service(model, qot, service_id, protected=False, avoid=avoid)
    cands = tuple(RestorationCandidate(
        lever=c.lever, reused_lightpaths=c.reused_lightpaths,
        new_lightpaths=c.new_lightpaths, restored_gbps=c.restored_gbps,
        shortfall_gbps=c.shortfall_gbps, cost_vector=c.cost_vector)
        for c in rs.candidates)
    return RestorationResult(rs.status, service_id, rs.demand_gbps, cands)
```

Delete the now-dead `_candidate` proxy-facet builder and the S7-11 proxy comment. In `views.py` `restoration_result_dict._cand`, change `"cost_facets": dict(c.cost_facets)` to `"cost_vector": dict(c.cost_vector)`.

- [ ] **Step 4: Run tests — expect PASS**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_restoration.py tests/test_server_phase8.py -q`
Expected: PASS. Then run the fuller server suite to catch stragglers:
Run: `conda run -n multilayer-optical-mcp python -m pytest tests/test_server_phase8.py tests/model -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/restoration.py src/multilayer_optical_mcp/model/views.py tests/test_server_phase8.py
git commit -m "refactor(restoration): delegate to route_service; retire proxy cost_facets for cost_vector"
```

---

### Task 6: Rebase `solve_allocation` onto the layered engine (packer)

**Files:**
- Modify: `src/multilayer_optical_mcp/model/allocation.py`
- Test: `tests/model/test_allocation_rebase.py` (new); existing `tests/model/test_rsa.py` / allocation tests must still pass.

**Interfaces:**
- Consumes: `build_layered_graph`, `place_demands`; `multilayer_disjoint.disjoint_pairs`; `objective.apply_candidate`; existing `solve_allocation` demand/result types.
- Produces: `solve_allocation` unchanged signature and typed `solution`/`partial`/`no_solution` result, now routing on the layered graph and consuming (commit each placement before the next demand).

- [ ] **Step 1: Write the two behavioural tests (greenfield still packs; scarcity grooms)**

```python
# tests/model/test_allocation_rebase.py
def test_greenfield_still_packs(greenfield_model_and_demands):
    model, demands, inv = greenfield_model_and_demands   # empty net
    res = solve_allocation(model, demands, spare_inventory=inv, ...)
    assert res.status in (SolverStatus.SOLUTION, SolverStatus.PARTIAL)
    # empty net -> LPE edges vanish -> new-lightpath-only placements
    assert all(p.reused_lightpaths == () for p in placed_placements(res))

def test_scarcity_grooms_onto_survivor(loaded_scarce_model_and_demand):
    # a survivor lightpath has residual capacity; transponders exhausted
    res = solve_allocation(model, [demand], spare_inventory={...: 0}, ...)
    # grooms rather than returning a false no_solution
    assert res.status is not SolverStatus.NO_SOLUTION
```

- [ ] **Step 2: Run — expect fail (still flat-graph greenfield-only)**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_allocation_rebase.py -q`
Expected: FAIL (scarcity groom returns no_solution under the flat greenfield packer).

- [ ] **Step 3: Rebase the packer**

Retire the flat-graph `_place_protected` / `compute_disjoint_paths` path. Loop demands in the existing weighted order; for each demand rebuild the layered graph on the **working** (consuming) model, harvest via `place_demands` (`groom_or_new` + `new_only`), take the **shortest-available** local pick (the frontier's first/lowest-weight placement — no `evaluate_objective` in the hot loop), and **commit it** onto the working state via `objective.apply_candidate(work, placement, demand_as_service)` so spectrum/inventory decrement before the next demand. For a protected demand, call `disjoint_pairs(work, placements, ...)` and take the best pair, committing both legs. Preserve typed `solution`/`partial` (placed vs. unplaced demands) / `no_solution`; a budget overrun returns best-so-far as structured data, never an exception.

Key rules to hold (from CLAUDE.md + spec §4): consume axis stays in the packer; greenfield is agentless (no `evaluate_objective` required for the fill); demands are supplied as fixture lists until the sibling traffic spec lands.

- [ ] **Step 4: Run rebase + existing allocation/rsa tests — expect PASS**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/model/test_allocation_rebase.py tests/model/test_rsa.py tests/test_server_phase4_rsa.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/allocation.py tests/model/test_allocation_rebase.py
git commit -m "feat(allocation): rebase solve_allocation packer onto the layered engine + disjoint_pairs"
```

---

### Task 7: Tool exposure — `route_service` + `evaluate_objective`

**Files:**
- Modify: `src/multilayer_optical_mcp/model/views.py` (add serializers)
- Modify: `src/multilayer_optical_mcp/server.py` (register two tools)
- Test: `tests/test_server_phase8.py` (append tool-shape tests)

**Interfaces:**
- Consumes: `route_service.RouteServiceResult`, `objective.ObjectiveResult`; `server.make_adapter_evaluator`; `SnapshotStore.current()`.
- Produces: MCP tools `route_service(service_id, protected=False, basis="physical", level="link", best_effort=False, avoid=None, weights=None) -> dict` and `evaluate_objective(state=None, weights=None) -> dict`.

- [ ] **Step 1: Write serializer + tool-shape tests**

```python
# tests/test_server_phase8.py (append)
def test_route_service_result_dict_shape():
    from multilayer_optical_mcp.model.views import route_service_result_dict
    # build a RouteServiceResult with one candidate carrying cost_vector, assert keys:
    d = route_service_result_dict(res)
    assert d["status"] and d["service_id"] and "candidates" in d
    assert "cost_vector" in d["candidates"][0]

def test_evaluate_objective_result_dict_shape():
    from multilayer_optical_mcp.model.views import objective_result_dict
    d = objective_result_dict(obj)
    assert set(d) >= {"spectrum_used","transponders","max_util","dropped_traffic",
                      "added_latency","total_margin","services_at_risk","scalar"}

def test_route_service_and_evaluate_objective_tools_registered():
    app = build_app(); _seed(app)
    out = _call(app, "evaluate_objective")
    assert "scalar" in out
    menu = _call(app, "route_service", service_id="svc1")
    assert menu["status"]
```

- [ ] **Step 2: Run — expect fail**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/test_server_phase8.py -q`
Expected: FAIL.

- [ ] **Step 3: Add serializers (views.py) and register tools (server.py)**

`views.py`:
```python
def objective_result_dict(res) -> Dict[str, Any]:
    return {"spectrum_used": res.spectrum_used, "transponders": res.transponders,
            "max_util": res.max_util, "dropped_traffic": res.dropped_traffic,
            "added_latency": res.added_latency, "total_margin": res.total_margin,
            "services_at_risk": res.services_at_risk, "scalar": res.scalar}

def route_service_result_dict(res) -> Dict[str, Any]:
    def _new_lp(r):
        return {"oms_sequence": list(r.oms_sequence), "lam": r.lam,
                "mode_id": r.mode_id, "gsnr_db": r.gsnr_db, "bitrate_gbps": r.bitrate_gbps}
    def _cand(c):
        return {"lever": c.lever, "reused_lightpaths": list(c.reused_lightpaths),
                "new_lightpaths": [_new_lp(r) for r in c.new_lightpaths],
                "restored_gbps": c.restored_gbps, "shortfall_gbps": c.shortfall_gbps,
                "cost_vector": dict(c.cost_vector)}
    def _pair(p):
        return {"working": _cand(p.working), "protection": _cand(p.protection),
                "disjoint": p.disjoint, "shared_assets": list(p.shared_assets),
                "shared_groups": list(p.shared_groups), "cost_vector": dict(p.cost_vector)}
    return {"status": res.status.value, "service_id": res.service_id,
            "demand_gbps": res.demand_gbps, "protected": res.protected,
            "candidates": [_cand(c) for c in res.candidates],
            "pairs": [_pair(p) for p in res.pairs]}
```

`server.py` (nested inside `build_app`, imports inside the factory, following the `compute_restoration` pattern):
```python
    @app.tool()
    def route_service(service_id: str, protected: bool = False,
                      basis: str = "physical", level: str = "link",
                      best_effort: bool = False, avoid: dict | None = None,
                      weights: dict | None = None) -> dict:
        """Service-level routing/restoration menu on the layered graph. avoid=None ->
        first-time routing; avoid={assets?,risk_groups?} -> restoration. protected=True
        returns a disjoint-pair menu (best_effort -> min-overlap PARTIAL). Read-only."""
        model = snapshots.current()
        qot = make_adapter_evaluator(model, results)
        res = _route_service(model, qot, service_id, protected=protected, basis=basis,
                             level=level, best_effort=best_effort, avoid=avoid, weights=weights)
        return route_service_result_dict(res)

    @app.tool()
    def evaluate_objective(state: str | None = None, weights: dict | None = None) -> dict:
        """7-term cost vector + weighted scalar for a state (snapshot id; defaults to
        current). Terms are costs except total_margin (a benefit, subtracted)."""
        model = snapshots.get(state) if state else snapshots.current()
        return objective_result_dict(_evaluate_objective(model, weights))
```
(Confirm the snapshot-by-id accessor name against `SnapshotStore`; if it is `snapshots.get`, keep as written — otherwise use the store's id lookup used elsewhere in `server.py`.)

- [ ] **Step 4: Run — expect PASS, then full suite**

Run: `conda run -n multilayer-optical-mcp python -m pytest tests/test_server_phase8.py -q`
Expected: PASS.
Run: `conda run -n multilayer-optical-mcp python -m pytest -q`
Expected: PASS (whole suite green).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/views.py src/multilayer_optical_mcp/server.py tests/test_server_phase8.py
git commit -m "feat(server): expose route_service + evaluate_objective tools"
```

---

## Verification (end-to-end)

1. **Full suite green:** `conda run -n multilayer-optical-mcp python -m pytest -q`.
2. **Scoring↔commit no-drift (the key guard):** `tests/model/test_objective_scoring.py::test_score_candidate_matches_real_commit` — `score_candidate` on a clone equals `evaluate_objective` after the identical `apply_candidate` path.
3. **Footprint trap:** `test_multilayer_disjoint.py` — a reused LP and a new run over the same fiber intersect under `physical`/`srlg`/`risk_group`.
4. **Margin gate:** `test_objective_scoring.py::test_margin_negative_candidate_scores_dropped` — a below-threshold run scores dropped, not nominal.
5. **Back-compat:** `tests/model/test_restoration.py` passes unchanged against the delegating wrapper; `cost_vector` replaces `cost_facets` in the serialized dict.
6. **Tool smoke via the app:** in a REPL, `build_app()`, seed a snapshot, call `evaluate_objective` (returns the 7-term dict) and `route_service("svc1", protected=True, best_effort=True)` (returns a pair menu with `shared_assets`/`shared_groups` explaining any residual overlap).
7. **Determinism:** run the new test files twice; identical menu order.

## Self-review notes (spec coverage)

- Components 1–5 of the spec map to Tasks 1 (disjoint), 2+3 (objective+materialization), 4 (route_service), 6 (allocation rebase), 7 (tool exposure); Task 5 covers `compute_restoration` wrapper + `cost_facets`→`cost_vector`.
- Deferred per spec: traffic/demand generation (sibling spec — Task 6 uses fixture demands), λ-aware routing, global allocation optimisation, `get_telemetry`.
- Intentionally-simple definitions carried verbatim from the spec: `transponders = 2 × #lightpaths` (netted vs. spare inventory only when supplied); `added_latency` absolute (agent diffs states). `total_margin` is the sole benefit term (subtracted in the scalar).
