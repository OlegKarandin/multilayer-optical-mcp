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
