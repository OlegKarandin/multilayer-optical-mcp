"""solve_rsa + solve_allocation — routing+spectrum and greenfield allocation.

Both follow the route-first / mode-from-SNR contract: route by total fiber
length, evaluate GSNR on that path via the GNPy adapter (through the
`QotEvaluator` seam), and accept iff at least one mode is feasible — the
delivered mode is the highest-bitrate one the GSNR supports, gated on the worse
of the two directions. With a single transponder type, GSNR is mode-independent,
so one QoT call per direction suffices.

Outcomes are typed (`solution`/`partial`/`no_solution`); resource exhaustion is
a typed result, never an exception. `solve_allocation` is greenfield only —
grooming onto existing lightpaths is Step 5.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from .assets import Direction
from .network import NetworkModel
from .qot import QoTState
from .solvers import (
    OmsPath, SolverStatus, compute_paths, compute_disjoint_paths,
)
from .spectrum import (
    SpectrumGrid, build_spectrum_state, first_fit_slot, occupied_along, reserve,
)
from ..gnpy_adapter.loading import Channel, LoadingState

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


def make_adapter_evaluator(model, store, *, topo_path=None, eqpt_path=None) -> QotEvaluator:
    """A QotEvaluator bound to the real GNPy adapter + a results store."""
    from ..gnpy_adapter.adapter import compute_qot  # lazy: avoids import cycle

    def _eval(*, oms_sequence, direction, mode_id, loading):
        state, _ = compute_qot(
            model=model, store=store, oms_sequence=tuple(oms_sequence),
            direction=direction, mode_id=mode_id, loading=loading,
            topo_path=topo_path, eqpt_path=eqpt_path,
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


# RSA and allocation share the result shape.
RSAResult = PlacementResult
AllocationResult = PlacementResult


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
) -> LoadingState:
    """Probe channel at *probe_slot* plus the channels already lit on the path's
    OMS (its WDM neighbors, which set NLI). Probe goes first so the adapter
    identifies it."""
    occ = occupied_along(state, oms_sequence)
    probe = Channel(grid.freq(probe_slot), grid.spacing_hz, None, ref_mode_id)
    neighbors = tuple(
        Channel(grid.freq(s), grid.spacing_hz, None, ref_mode_id)
        for s in range(grid.num_slots)
        if s != probe_slot and (occ >> s) & 1
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
) -> Optional[SpectrumAssignment]:
    """First-fit slot on *route* + the best feasible mode meeting *require_gbps*
    (if any). Does not mutate *state*."""
    slot = first_fit_slot(state, route.oms_sequence, grid)
    if slot is None:
        return None
    loading = _build_loading(grid, state, route.oms_sequence, slot, ref_mode_id)
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
) -> Optional[SpectrumAssignment]:
    """Among the candidate routes (length-ordered), choose the feasible
    assignment with the lowest slot index — spreading demands across disjoint
    routes at low slots rather than packing higher slots onto one route, which
    keeps total spectrum usage down. Ties keep the shorter (earlier) route."""
    routes = compute_paths(model, src, dst, _ROUTE_CAP, weight="length").paths
    best: Optional[SpectrumAssignment] = None
    for route in routes:
        sa = _assign_on_route(model, qot, route, state, grid, ref_mode_id, require_gbps)
        if sa is not None and (best is None or sa.slot_index < best.slot_index):
            best = sa
    if best is None:
        return None
    reserve(state, best.oms_path.oms_sequence, best.slot_index)
    return best


def _place_protected(
    model, qot, src, dst, state, grid, ref_mode_id, require_gbps,
    basis, level, best_effort,
) -> Optional[Tuple[SpectrumAssignment, SpectrumAssignment]]:
    dr = compute_disjoint_paths(model, src, dst, basis, level, best_effort,
                                weight="length")
    if dr.path_a is None or dr.path_b is None:
        return None
    trial = dict(state)  # tentative: only commit if both legs place
    wa = _assign_on_route(model, qot, dr.path_a, trial, grid, ref_mode_id, require_gbps)
    if wa is None:
        return None
    reserve(trial, dr.path_a.oms_sequence, wa.slot_index)
    wb = _assign_on_route(model, qot, dr.path_b, trial, grid, ref_mode_id, require_gbps)
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
) -> RSAResult:
    """Route + spectrum-assign each demand. Demand:
    `{id, src, dst, protected?, required_gbps?, constraints?}` (src/dst = optical
    node ids). Protected demands get a disjoint working+protection pair under the
    demand's basis/level (default physical/link)."""
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
                                    require, basis, level, be)
            if pair is None:
                unplaced.append((did, "no disjoint feasible pair"))
                continue
            placements.append(DemandPlacement(did, pair[0], pair[1]))
        else:
            sa = _place_unprotected(model, qot, src, dst, work, grid, ref_mode, require)
            if sa is None:
                unplaced.append((did, "no feasible route/slot/mode"))
                continue
            placements.append(DemandPlacement(did, sa, None))

    return PlacementResult(_status(len(placements), len(unplaced)),
                           tuple(placements), tuple(unplaced))


# ----------------------------------------------------------------- solve_allocation

def solve_allocation(
    model: NetworkModel, qot: QotEvaluator, demands: Sequence[dict],
    spare_inventory: Dict[str, int], objective: str = "max_placed",
    weights: Optional[Dict[str, float]] = None,
) -> AllocationResult:
    """Greenfield heuristic: light new lightpaths from a per-site transponder
    count to serve as many weighted demands as possible. Demand:
    `{id, src, dst, demand_gbps, protected?, constraints?}`. A demand is placed
    iff a single lightpath's best feasible mode bitrate ≥ demand_gbps and both
    endpoint sites have a spare transponder (two lightpaths / four transponders
    when protected). Never aborts — unplaced demands come back as a typed
    `partial`/`no_solution`."""
    grid = SpectrumGrid.default()
    work = build_spectrum_state(model, grid)
    ref_mode = model.modes.list()[0].id
    inv = dict(spare_inventory)
    weights = weights or {}
    ordered = sorted(demands, key=lambda d: (-weights.get(d["id"], 0.0), d["id"]))

    placements: List[DemandPlacement] = []
    unplaced: List[Tuple[str, str]] = []

    for d in ordered:
        did, src, dst = d["id"], d["src"], d["dst"]
        gbps = d["demand_gbps"]
        protected = d.get("protected", False)
        n_lp = 2 if protected else 1  # lightpaths -> transponders per endpoint site
        if inv.get(src, 0) < n_lp or inv.get(dst, 0) < n_lp:
            unplaced.append((did, "insufficient transponders"))
            continue

        if protected:
            basis, level, be = _demand_constraints(d)
            pair = _place_protected(model, qot, src, dst, work, grid, ref_mode,
                                    gbps, basis, level, be)
            if pair is None:
                unplaced.append((did, "no disjoint feasible pair meeting demand"))
                continue
            inv[src] -= 2
            inv[dst] -= 2
            placements.append(DemandPlacement(did, pair[0], pair[1]))
        else:
            sa = _place_unprotected(model, qot, src, dst, work, grid, ref_mode, gbps)
            if sa is None:
                unplaced.append((did, "no feasible route/slot/mode meeting demand"))
                continue
            inv[src] -= 1
            inv[dst] -= 1
            placements.append(DemandPlacement(did, sa, None))

    return PlacementResult(_status(len(placements), len(unplaced)),
                           tuple(placements), tuple(unplaced))
