"""solve_rsa + solve_allocation — routing+spectrum and greenfield allocation.

Both follow the route-first / mode-from-SNR contract: route by total fiber
length, evaluate GSNR on that path via the GNPy adapter (through the
`QotEvaluator` seam), and accept iff at least one mode is feasible — the
delivered mode is the highest-bitrate one the GSNR supports, gated on the worse
of the two directions. With a single transponder type, GSNR is mode-independent,
so one QoT call per direction suffices.

Outcomes are typed (`solution`/`partial`/`no_solution`); resource exhaustion is
a typed result, never an exception.

`solve_rsa` stays on the flat OMS graph (spectrum-slot assignment). `solve_allocation`
is rebased onto the LAYERED engine (`build_layered_graph`/`place_demands`) so it can
groom demands onto surviving lightpaths' residual capacity, consuming across demands
by synthesizing a Service per demand on a clone and committing it through the real
`objective.apply_candidate` machinery.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from .assets import Direction, Service
from .network import NetworkModel
from .qot import QoTState
from .solvers import (
    OmsPath, SolverStatus, compute_paths, compute_disjoint_paths,
)
from .spectrum import (
    SpectrumGrid, build_spectrum_state, first_fit_slot, occupied_along, reserve,
    FillPolicy,
)
from .multilayer_graph import build_layered_graph, place_demands, NewLightpathRun
from .multilayer_disjoint import disjoint_pairs
from .restoration import _lever
from . import objective as _objective
from ..gnpy_adapter.loading import Channel, LoadingState
from ..gnpy_adapter.adapter import compute_qot, harvest_qot, harvest_cache_key

# Candidate routes considered per demand when searching for a feasible placement.
_ROUTE_CAP = 8


class QotEvaluator(Protocol):
    """Seam the solvers reach QoT through — the adapter is the only thing that
    talks to GNPy. `make_adapter_evaluator` builds the real one; tests may pass
    a deterministic fake."""
    def __call__(
        self, *, oms_sequence: Tuple[str, ...], direction: Direction,
        mode_id: str, loading: LoadingState,
    ) -> QoTState: ...


def make_adapter_evaluator(model, store, *, topo_path=None, eqpt_path=None,
                           cache=None, harvest_cache=None) -> QotEvaluator:
    """A QotEvaluator bound to the real GNPy adapter + a results store. An optional
    `cache` (QoTCache) memoizes propagation across calls — content-addressed, so it
    is safe to share one cache across a whole solve/settle run.

    An optional `harvest_cache` (HarvestCache) additionally detects FULL-policy's
    full-grid probe loading (every grid slot lit) and routes it through
    `harvest_qot` instead of `compute_qot`: one propagation harvests every
    carrier's GSNR, so the many probe-slot calls FillPolicy.FULL makes across a
    solve/settle run collapse into one propagation per (path, direction, mode,
    physical-fingerprint) instead of one per probe. Any non-full (subset/ACTUAL)
    loading, or a topo/eqpt-file run (not model-fingerprintable), falls through to
    today's per-call `compute_qot` path unchanged."""
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
                # probe = the channel compute_qot would pick when no center_freq_hz
                # is given (first mode_id match = channels[0] under FULL, which
                # prepends the probe) -- same selection rule as compute_qot's.
                probe = next(c for c in loading.channels if c.mode_id == mode_id)
                probe_slot = grid.slot_of(probe.center_freq_hz)
                if probe_slot in vec:
                    return vec[probe_slot]
                # The probe's own slot is one an amp/ROADM band-edge filter demuxed
                # out of the harvest (harvest_qot's dict is not guaranteed complete
                # -- e.g. this repo's grid/amp-band config always drops the topmost
                # slot). Fall back to compute_qot for this one call rather than
                # raising a KeyError; compute_qot has this same underlying
                # band-edge limitation for such a probe, so this does not
                # introduce new incorrect behavior.
        state, _ = compute_qot(
            model=model, store=store, oms_sequence=tuple(oms_sequence),
            direction=direction, mode_id=mode_id, loading=loading,
            topo_path=topo_path, eqpt_path=eqpt_path, cache=cache,
        )
        return state
    return _eval


# ----------------------------------------------------------------- result types

@dataclass(frozen=True)
class SpectrumAssignment:
    oms_path: OmsPath
    slot_index: int
    center_freq_hz: float
    mode_id: str
    gsnr_db: float


@dataclass(frozen=True)
class DemandPlacement:
    demand_id: str
    working: SpectrumAssignment
    protection: Optional[SpectrumAssignment] = None


@dataclass(frozen=True)
class PlacementResult:
    status: SolverStatus
    placements: Tuple[DemandPlacement, ...] = ()
    unplaced: Tuple[Tuple[str, str], ...] = ()  # (demand_id, reason)


# solve_rsa's result shape (flat, slot-based).
RSAResult = PlacementResult


# solve_allocation's result shape (layered, Placement-based). A groomed leg reuses
# an existing lightpath (no transponder, no new spectrum); a new leg lights one.
# The slot-based SpectrumAssignment cannot represent reuse, so allocation gets its
# own Placement-shaped result rather than aliasing PlacementResult.
@dataclass(frozen=True)
class AllocationPlacement:
    demand_id: str
    lever: str                                     # restoration._lever(working placement)
    reused_lightpaths: Tuple[str, ...]
    new_lightpaths: Tuple[NewLightpathRun, ...]
    protection_reused: Tuple[str, ...] = ()        # protected demands only
    protection_new: Tuple[NewLightpathRun, ...] = ()
    restored_gbps: float = 0.0
    shortfall_gbps: float = 0.0


@dataclass(frozen=True)
class AllocationResult:
    status: SolverStatus
    placements: Tuple[AllocationPlacement, ...] = ()
    unplaced: Tuple[Tuple[str, str], ...] = ()     # (demand_id, reason)


def _status(n_placed: int, n_unplaced: int) -> SolverStatus:
    if n_unplaced == 0:
        return SolverStatus.SOLUTION
    if n_placed == 0:
        return SolverStatus.NO_SOLUTION
    return SolverStatus.PARTIAL


# ----------------------------------------------------------------- core placement

def _build_loading(
    grid: SpectrumGrid, state: Dict[str, int], oms_sequence: Tuple[str, ...],
    probe_slot: int, ref_mode_id: str,
    fill_policy: FillPolicy = FillPolicy.ACTUAL,
) -> LoadingState:
    """Probe channel at *probe_slot* plus its WDM neighbors (which set NLI). Probe
    goes first so the adapter identifies it.

    Under ``ACTUAL`` the neighbors are the channels already lit on the path's OMS
    (a subset of the eventual load). Under ``FULL`` every other grid slot is lit,
    regardless of occupancy, so the probe sees the worst-case fully-loaded comb —
    the mode chosen then stays feasible as the network fills (see FillPolicy)."""
    if fill_policy is FillPolicy.FULL:
        include = lambda s: True                       # every non-probe slot
    else:
        occ = occupied_along(state, oms_sequence)
        include = lambda s: bool((occ >> s) & 1)       # only lit slots
    probe = Channel(grid.freq(probe_slot), grid.spacing_hz, None, ref_mode_id)
    neighbors = tuple(
        Channel(grid.freq(s), grid.spacing_hz, None, ref_mode_id)
        for s in range(grid.num_slots)
        if s != probe_slot and include(s)
    )
    return LoadingState((probe,) + neighbors)


def _best_feasible_mode(
    model: NetworkModel, qot: QotEvaluator, oms_sequence: Tuple[str, ...],
    loading: LoadingState, ref_mode_id: str,
):
    """Worse-direction GSNR → highest-bitrate mode under threshold (or None)."""
    gsnr = min(
        qot(oms_sequence=oms_sequence, direction=d,
            mode_id=ref_mode_id, loading=loading).gsnr_db
        for d in (Direction.FORWARD, Direction.BACKWARD)
    )
    feasible = [m for m in model.modes.list() if m.required_gsnr_db <= gsnr]
    if not feasible:
        return None, gsnr
    return max(feasible, key=lambda m: m.bitrate_gbps), gsnr


def _assign_on_route(
    model: NetworkModel, qot: QotEvaluator, route: OmsPath,
    state: Dict[str, int], grid: SpectrumGrid, ref_mode_id: str,
    require_gbps: Optional[float],
    fill_policy: FillPolicy = FillPolicy.ACTUAL,
) -> Optional[SpectrumAssignment]:
    """First-fit slot on *route* + the best feasible mode meeting *require_gbps*
    (if any). Does not mutate *state*."""
    slot = first_fit_slot(state, route.oms_sequence, grid)
    if slot is None:
        return None
    loading = _build_loading(grid, state, route.oms_sequence, slot, ref_mode_id,
                             fill_policy)
    mode, gsnr = _best_feasible_mode(model, qot, route.oms_sequence, loading, ref_mode_id)
    if mode is None:
        return None
    if require_gbps is not None and mode.bitrate_gbps < require_gbps:
        return None
    return SpectrumAssignment(
        oms_path=route, slot_index=slot, center_freq_hz=grid.freq(slot),
        mode_id=mode.id, gsnr_db=gsnr,
    )


def _place_unprotected(
    model, qot, src, dst, state, grid, ref_mode_id, require_gbps,
    fill_policy: FillPolicy = FillPolicy.ACTUAL,
) -> Optional[SpectrumAssignment]:
    """Among the candidate routes (length-ordered), choose the feasible
    assignment with the lowest slot index — spreading demands across disjoint
    routes at low slots rather than packing higher slots onto one route, which
    keeps total spectrum usage down. Ties keep the shorter (earlier) route."""
    routes = compute_paths(model, src, dst, _ROUTE_CAP, weight="length").paths
    best: Optional[SpectrumAssignment] = None
    for route in routes:
        sa = _assign_on_route(model, qot, route, state, grid, ref_mode_id, require_gbps,
                              fill_policy)
        if sa is not None and (best is None or sa.slot_index < best.slot_index):
            best = sa
    if best is None:
        return None
    reserve(state, best.oms_path.oms_sequence, best.slot_index)
    return best


def _place_protected(
    model, qot, src, dst, state, grid, ref_mode_id, require_gbps,
    basis, level, best_effort,
    fill_policy: FillPolicy = FillPolicy.ACTUAL,
) -> Optional[Tuple[SpectrumAssignment, SpectrumAssignment]]:
    dr = compute_disjoint_paths(model, src, dst, basis, level, best_effort,
                                weight="length")
    if dr.path_a is None or dr.path_b is None:
        return None
    trial = dict(state)  # tentative: only commit if both legs place
    wa = _assign_on_route(model, qot, dr.path_a, trial, grid, ref_mode_id, require_gbps,
                          fill_policy)
    if wa is None:
        return None
    reserve(trial, dr.path_a.oms_sequence, wa.slot_index)
    wb = _assign_on_route(model, qot, dr.path_b, trial, grid, ref_mode_id, require_gbps,
                          fill_policy)
    if wb is None:
        return None
    reserve(trial, dr.path_b.oms_sequence, wb.slot_index)
    state.clear()
    state.update(trial)
    return wa, wb


def _demand_constraints(d: dict) -> Tuple[str, str, bool]:
    c = d.get("constraints", {}) or {}
    return c.get("basis", "physical"), c.get("level", "link"), c.get("best_effort", False)


# ----------------------------------------------------------------- solve_rsa

def solve_rsa(
    model: NetworkModel, qot: QotEvaluator, demands: Sequence[dict],
    objective: str = "shortest", constraints: Optional[dict] = None,
    fill_policy: FillPolicy = FillPolicy.ACTUAL,
) -> RSAResult:
    """Route + spectrum-assign each demand. Demand:
    `{id, src, dst, protected?, required_gbps?, constraints?}` (src/dst = optical
    node ids). Protected demands get a disjoint working+protection pair under the
    demand's basis/level (default physical/link).

    `fill_policy` picks the acceptance-probe reference loading (ACTUAL default;
    FULL for margin-stable, order-independent mode selection — see FillPolicy)."""
    grid = SpectrumGrid.default()
    work = build_spectrum_state(model, grid)
    ref_mode = model.modes.list()[0].id
    placements: List[DemandPlacement] = []
    unplaced: List[Tuple[str, str]] = []

    for d in demands:
        did, src, dst = d["id"], d["src"], d["dst"]
        require = d.get("required_gbps")
        if d.get("protected", False):
            basis, level, be = _demand_constraints(d)
            pair = _place_protected(model, qot, src, dst, work, grid, ref_mode,
                                    require, basis, level, be, fill_policy)
            if pair is None:
                unplaced.append((did, "no disjoint feasible pair"))
                continue
            placements.append(DemandPlacement(did, pair[0], pair[1]))
        else:
            sa = _place_unprotected(model, qot, src, dst, work, grid, ref_mode, require,
                                    fill_policy)
            if sa is None:
                unplaced.append((did, "no feasible route/slot/mode"))
                continue
            placements.append(DemandPlacement(did, sa, None))

    return PlacementResult(_status(len(placements), len(unplaced)),
                           tuple(placements), tuple(unplaced))


# ----------------------------------------------------------------- solve_allocation
#
# Layered, consuming, grooming-aware. Unlike solve_rsa (flat, one-shot per demand),
# the allocator commits each placement onto a CLONE before the next demand routes,
# so a groomed lightpath's residual shrinks and demand N+1 sees the reduced
# headroom demand N used — the whole grooming-consumption mechanism, obtained for
# free by reusing objective.apply_candidate (which reroutes the synthetic service
# and thereby registers its IP-layer load).


def _tp_need(placement) -> Dict[str, int]:
    """Transponders a placement consumes: one per NEW-lightpath run endpoint site.
    Reused (groomed) legs light no lightpath and cost zero transponders — the
    scarcity win."""
    need: Dict[str, int] = {}
    for run in placement.new_lightpaths:
        need[run.src_node] = need.get(run.src_node, 0) + 1
        need[run.dst_node] = need.get(run.dst_node, 0) + 1
    return need


def _inv_ok(inv: Dict[str, int], need: Dict[str, int]) -> bool:
    return all(inv.get(s, 0) >= n for s, n in need.items())


def _dec_inv(inv: Dict[str, int], need: Dict[str, int]) -> None:
    for s, n in need.items():
        inv[s] = inv.get(s, 0) - n


def _merge_need(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    out = dict(a)
    for s, n in b.items():
        out[s] = out.get(s, 0) + n
    return out


def _harvest_alloc(model, qot, g, src, dst, demand_gbps, k=8,
                   fill_policy: FillPolicy = FillPolicy.ACTUAL):
    """The route_service harvest: groom_or_new + new_only frontiers on the CURRENT
    (consumed) loading, deduped on the λ-free route identity, materializable only.
    Cost order preserved — the frontier's discovery order IS 'shortest-available
    per demand', so grooming (cheap LPE) precedes lighting new (moderate TxE)."""
    out: List = []
    seen: set = set()
    for policy in ("groom_or_new", "new_only"):
        for p in place_demands(model, g, qot, src=src, dst=dst,
                               demand_gbps=demand_gbps, policy=policy, k=k,
                               fill_policy=fill_policy):
            key = (p.reused_lightpaths, tuple(r.oms_sequence for r in p.new_lightpaths))
            if key in seen:
                continue
            seen.add(key)
            if not _objective.placement_materializable(model, p):
                continue
            out.append(p)
    return out


def solve_allocation(
    model: NetworkModel, qot: QotEvaluator, demands: Sequence[dict],
    spare_inventory: Dict[str, int], objective: str = "max_placed",
    weights: Optional[Dict[str, float]] = None,
    fill_policy: FillPolicy = FillPolicy.ACTUAL,
) -> AllocationResult:
    """Weighted, consuming heuristic packer over the layered engine. Demand:
    `{id, src, dst, demand_gbps, protected?, constraints?}` (src/dst = optical node
    ids). Demands are placed in weight order; each demand routes over the CURRENT
    consumed state, so it grooms onto a survivor's residual capacity when one is
    reachable (costing no transponder) and lights a new lightpath otherwise
    (costing one transponder per new-run endpoint, gated on spare inventory). A
    protected demand gets a disjoint working+protection pair under the demand's
    basis/level. Never aborts — inventory/feasibility/route misses are typed
    `unplaced` entries yielding `partial`/`no_solution`.

    `weights` here is PER-DEMAND PLACEMENT PRIORITY: maps a demand id to an
    ordering weight (higher = placed first); it does not feed
    evaluate_objective's cost vector (that's route_service's/evaluate_objective's
    `weights`, a different meaning of the same parameter name)."""
    return _pack(model, qot, demands, spare_inventory, weights, fill_policy)[0]


def solve_allocation_model(
    model: NetworkModel, qot: QotEvaluator, demands: Sequence[dict],
    spare_inventory: Dict[str, int],
    weights: Optional[Dict[str, float]] = None,
    fill_policy: FillPolicy = FillPolicy.ACTUAL,
) -> Tuple[AllocationResult, NetworkModel]:
    """As `solve_allocation`, but also returns the fully-loaded `work` clone the
    packer built — lightpaths lit, IP links bound, services carrying load. `work`
    is provisioned through the canonical `apply_op(ProvisionLightpath)` path (via
    `objective.apply_candidate`), so it is byte-for-byte the state a real commit of
    the same placements would produce; ground truth (`model`) is untouched. This is
    the materialization the operating-network builder (`model/scenario.py`)
    consumes instead of re-provisioning from the result."""
    return _pack(model, qot, demands, spare_inventory, weights, fill_policy)


def _pack(
    model: NetworkModel, qot: QotEvaluator, demands: Sequence[dict],
    spare_inventory: Dict[str, int],
    weights: Optional[Dict[str, float]],
    fill_policy: FillPolicy = FillPolicy.ACTUAL,
) -> Tuple[AllocationResult, NetworkModel]:
    """Shared packing core: consume demands on a clone, returning both the typed
    `AllocationResult` and the loaded clone. `solve_allocation` drops the clone;
    `solve_allocation_model` keeps it."""
    work = model.clone()                 # consume on a clone; ground truth untouched
    site_to_router = {r.site: r.id for r in work.list_routers()}
    inv = dict(spare_inventory)
    weights = weights or {}
    ordered = sorted(demands, key=lambda d: (-weights.get(d["id"], 0.0), d["id"]))

    placements: List[AllocationPlacement] = []
    unplaced: List[Tuple[str, str]] = []

    for d in ordered:
        did, src, dst = d["id"], d["src"], d["dst"]
        gbps = d["demand_gbps"]
        protected = d.get("protected", False)

        if src not in site_to_router or dst not in site_to_router:
            unplaced.append((did, "no router at endpoint"))
            continue

        # Model the demand as a Service on `work`; committing it via apply_candidate
        # reroutes it, registering IP load so the next demand sees reduced residual.
        svc = Service(id=did, src_router=site_to_router[src],
                      dst_router=site_to_router[dst], demand_gbps=gbps,
                      working_path=())
        work.add_service(svc)

        g = build_layered_graph(work)
        cands = _harvest_alloc(work, qot, g, src, dst, gbps, fill_policy=fill_policy)
        if not cands:
            unplaced.append((did, "no feasible route"))
            continue

        if protected:
            basis, level, be = _demand_constraints(d)
            pp = disjoint_pairs(work, cands, basis=basis, level=level,
                                best_effort=be, top_n=1)
            if not pp:
                unplaced.append((did, "no disjoint feasible pair"))
                continue
            pair = pp[0]
            need = _merge_need(_tp_need(pair.working), _tp_need(pair.protection))
            if not _inv_ok(inv, need):
                unplaced.append((did, "insufficient transponders"))
                continue
            _objective.apply_candidate(work, pair.working, svc)          # provision+seed+reroute
            _objective.provision_new_runs(work, pair.protection, svc, prefix="prot")
            _dec_inv(inv, need)
            placements.append(AllocationPlacement(
                demand_id=did, lever=_lever(pair.working),
                reused_lightpaths=pair.working.reused_lightpaths,
                new_lightpaths=pair.working.new_lightpaths,
                protection_reused=pair.protection.reused_lightpaths,
                protection_new=pair.protection.new_lightpaths,
                restored_gbps=pair.working.restored_gbps,
                shortfall_gbps=pair.working.shortfall_gbps))
        else:
            pick = cands[0]                       # shortest-available local pick
            need = _tp_need(pick)
            if not _inv_ok(inv, need):
                unplaced.append((did, "insufficient transponders"))
                continue
            _objective.apply_candidate(work, pick, svc)                  # provision+seed+reroute
            _dec_inv(inv, need)
            placements.append(AllocationPlacement(
                demand_id=did, lever=_lever(pick),
                reused_lightpaths=pick.reused_lightpaths,
                new_lightpaths=pick.new_lightpaths,
                restored_gbps=pick.restored_gbps,
                shortfall_gbps=pick.shortfall_gbps))

    return (AllocationResult(_status(len(placements), len(unplaced)),
                             tuple(placements), tuple(unplaced)), work)
