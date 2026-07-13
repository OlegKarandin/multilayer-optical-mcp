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
from typing import Dict, FrozenSet, Optional, Tuple

from .network import NetworkModel
from .solvers import SolverStatus
from .multilayer_graph import NewLightpathRun, Placement
# route_service imports _forbidden_assets/_lever from this module, so the
# dependency is inverted here (compute_restoration -> route_service) via a
# deferred import inside the function to avoid a circular import at load time.


@dataclass(frozen=True)
class RestorationCandidate:
    lever: str                              # "ip_reroute" | "optical_reroute" | "hybrid"
    reused_lightpaths: Tuple[str, ...]
    new_lightpaths: Tuple[NewLightpathRun, ...]
    restored_gbps: float
    shortfall_gbps: float
    cost_vector: Dict[str, float]           # route_service's 7-term objective + "scalar"


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


def compute_restoration(
    model: NetworkModel, qot, service_id: str, avoid: Optional[dict] = None,
) -> RestorationResult:
    """Enumerate recovery candidates for a service over survivors. `avoid` is
    `{assets?: [...], risk_groups?: [...]}` (typically inject_failure's set).

    Thin wrapper over route_service's single-candidate (unprotected) menu: the
    harvest, dedup, and materializability filtering all live there now. Ranking
    is re-sorted here on (shortfall_gbps, cost_vector["scalar"]) rather than
    route_service's plain scalar order, because restoration must still surface a
    FULLY-restoring candidate ahead of a cheaper-but-partial one.
    """
    from .route_service import route_service  # deferred: avoids circular import

    rs = route_service(model, qot, service_id, protected=False, avoid=avoid)
    candidates = tuple(
        RestorationCandidate(
            lever=c.lever,
            reused_lightpaths=c.reused_lightpaths,
            new_lightpaths=c.new_lightpaths,
            restored_gbps=c.restored_gbps,
            shortfall_gbps=c.shortfall_gbps,
            cost_vector=c.cost_vector,
        )
        for c in rs.candidates
    )
    candidates = tuple(sorted(candidates,
                              key=lambda c: (c.shortfall_gbps, c.cost_vector["scalar"])))
    if not candidates:
        status = SolverStatus.NO_SOLUTION
    elif any(c.shortfall_gbps == 0.0 for c in candidates):
        status = SolverStatus.SOLUTION
    else:
        status = SolverStatus.PARTIAL
    return RestorationResult(status, service_id, rs.demand_gbps, candidates)
