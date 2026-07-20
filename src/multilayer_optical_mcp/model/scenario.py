"""Operating-network builder — turns a bare topology into a loaded steady state.

This is offline **setup / bootstrapping**, NOT an MCP tool: the server reoptimizes
an already-operating network, and this manufactures that pre-disaster operating
state. It drives the seeded gravity generator (`traffic.generate_demands`) and the
existing packer (`allocation.solve_allocation_model`) in a utilization-target
convergence loop, then hands back the packer's fully-provisioned clone as the
loaded `NetworkModel` (lightpaths lit, IP links bound, grooming populated).

Search: a scalar `scale` sets total offered load. Utilization rises with scale in
discrete jumps (quantized demands + integer lightpath placement), so this is a
bracketed best-effort search (exponential bracket, then bisection) returning the
largest scale whose mean IP-link utilization is at/under target AND whose busiest
link is under the cap — never an exact optimizer, never an exception (repo's typed
best-effort solver contract).

Materialization: the packer already provisions every demand through the canonical
`apply_op(ProvisionLightpath)` path, seeding each new lightpath's QoT from its
predicted GSNR. A final `settle` pass (`recompute_qot_under_loading`) replaces those
predicted seeds with the real per-OMS interferer comb, so the returned model's
IP-link capacities are ground truth. `settle` is an injectable seam: it uses the
real GNPy adapter, so QoT-free unit tests pass a no-op.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Callable, Dict, List, Optional

from .allocation import QotEvaluator, solve_allocation_model, AllocationResult
from .ip_routing import simulate_ip_routing
from .network import NetworkModel
from .solvers import SolverStatus
from .traffic import generate_demands


@dataclass(frozen=True)
class ScenarioReport:
    status: SolverStatus
    achieved_mean_util: float
    achieved_max_util: float
    n_demands: int
    total_offered_gbps: float
    transponders_used: int
    unplaced_count: int
    scale: float
    limit: str                    # "none" | "max_util_cap" | "spare_inventory"


@dataclass(frozen=True)
class ScenarioResult:
    model: NetworkModel           # THE artifact — the loaded operating steady state
    demands: List[dict]           # provenance: the demand list that produced it
    report: ScenarioReport


@dataclass
class _Sample:
    scale: float
    demands: List[dict]
    result: Optional[AllocationResult]
    work: NetworkModel
    mean_u: float
    max_u: float


def _default_settle(store) -> Callable[[NetworkModel], None]:
    """Settle QoT to ground truth via a full recompute under the committed comb."""
    from ..gnpy_adapter.adapter import recompute_qot_under_loading
    from .whatif import loading_from_model

    def _settle(work: NetworkModel) -> None:
        recompute_qot_under_loading(
            model=work, store=store, loading=loading_from_model(work))
    return _settle


def build_operating_network(
    model: NetworkModel,
    *,
    seed: int,
    qot: QotEvaluator,
    target_mean_util: float = 0.6,
    max_util_cap: float = 0.95,
    unit_gbps: float = 100.0,
    protected_fraction: float = 0.3,
    alpha: float = 1.0,
    node_mass: Optional[Dict[str, float]] = None,
    mass_jitter: float = 0.15,
    pair_density: Optional[float] = None,
    spare_inventory: Optional[Dict[str, int]] = None,
    max_iters: int = 24,
    store=None,
    settle: Optional[Callable[[NetworkModel], None]] = None,
    tol: float = 0.02,
) -> ScenarioResult:
    """Build a loaded operating `NetworkModel` at ~`target_mean_util` mean IP-link
    utilization under a `max_util_cap` ceiling. See module docstring.

    `pair_density` is forwarded to `generate_demands`: `None` (default) keeps the
    full-matrix demand pattern; a value in (0, 1] sparsifies pair selection (see
    that function's docstring for the exact weighted-Bernoulli contract)."""
    sites = sorted({r.site for r in model.list_routers()})
    if len(sites) < 2:
        return ScenarioResult(model.clone(), [], ScenarioReport(
            SolverStatus.NO_SOLUTION, 0.0, 0.0, 0, 0.0, 0, 0, 0.0, "none"))

    # Generous default inventory: setup provisions to carry the load; scarcity is a
    # runtime/disaster concern, not a setup one.
    if spare_inventory is None:
        spare_inventory = {s: 10 ** 6 for s in sites}
    if store is None:
        from .qot_results import QoTResultStore
        store = QoTResultStore()
    if settle is None:
        settle = _default_settle(store)

    def _sample(scale: float) -> _Sample:
        demands = generate_demands(
            model, seed=seed, scale=scale, alpha=alpha, unit_gbps=unit_gbps,
            protected_fraction=protected_fraction, node_mass=node_mass,
            mass_jitter=mass_jitter, pair_density=pair_density)
        if not demands:
            return _Sample(scale, [], None, model.clone(), 0.0, 0.0)
        result, work = solve_allocation_model(
            model, qot, demands, dict(spare_inventory))
        us = [u.utilization for u in simulate_ip_routing(work).utilizations
              if u.utilization is not None]
        return _Sample(scale, demands, result, work,
                       mean(us) if us else 0.0, max(us) if us else 0.0)

    def _feasible(s: _Sample) -> bool:
        return s.max_u <= max_util_cap and s.mean_u <= target_mean_util + tol

    used = 0
    empty = _Sample(0.0, [], None, model.clone(), 0.0, 0.0)
    best = empty                 # best FEASIBLE sample (maximize mean_u toward target)
    lo = empty
    hi: Optional[_Sample] = None
    cap_bound = False

    # Exponential bracket: climb scale until mean reaches target or the cap binds.
    s = unit_gbps
    while used < max_iters:
        cur = _sample(s)
        used += 1
        if _feasible(cur) and cur.mean_u >= best.mean_u:
            best = cur
        if cur.max_u > max_util_cap:
            hi, cap_bound = cur, True
            break
        if cur.mean_u >= target_mean_util - tol:
            hi = cur
            break
        lo = cur
        s *= 2.0

    # Bisection between the last sub-target scale and the bracket top.
    if hi is not None:
        a, b = lo.scale, hi.scale
        while used < max_iters and (b - a) > unit_gbps * 0.5:
            mid = (a + b) / 2.0
            cur = _sample(mid)
            used += 1
            if _feasible(cur) and cur.mean_u >= best.mean_u:
                best = cur
            if cur.max_u > max_util_cap:
                cap_bound = True
                b = cur.scale
            elif cur.mean_u > target_mean_util:
                b = cur.scale
            else:
                a = cur.scale

    settle(best.work)

    reached = best.mean_u >= target_mean_util - tol
    unplaced = len(best.result.unplaced) if best.result else 0
    if best.result is None:
        status, limit = SolverStatus.NO_SOLUTION, "none"
    elif reached and best.max_u <= max_util_cap:
        status, limit = SolverStatus.SOLUTION, "none"
    elif cap_bound:
        status, limit = SolverStatus.PARTIAL, "max_util_cap"
    elif unplaced:
        status, limit = SolverStatus.PARTIAL, "spare_inventory"
    else:
        # Below target but nothing hard-bound it: a quantization plateau (util
        # jumps in discrete steps as grooming concentrates), not a real limit.
        status, limit = SolverStatus.PARTIAL, "none"

    transponders = _count_transponders(best.result)
    report = ScenarioReport(
        status=status,
        achieved_mean_util=best.mean_u,
        achieved_max_util=best.max_u,
        n_demands=len(best.demands),
        total_offered_gbps=sum(d["demand_gbps"] for d in best.demands),
        transponders_used=transponders,
        unplaced_count=unplaced,
        scale=best.scale,
        limit=limit,
    )
    return ScenarioResult(best.work, best.demands, report)


def _count_transponders(result: Optional[AllocationResult]) -> int:
    """One transponder per new-lightpath endpoint across all placements (working +
    protection legs). Groomed reuse costs none."""
    if result is None:
        return 0
    n = 0
    for p in result.placements:
        n += 2 * (len(p.new_lightpaths) + len(p.protection_new))
    return n
