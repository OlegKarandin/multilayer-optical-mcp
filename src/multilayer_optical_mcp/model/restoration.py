# src/multilayer_optical_mcp/model/restoration.py
"""Per-service restoration: enumerate recovery candidates over survivors.

Read-only. Prunes the layered graph by an avoid-set (failed assets / SRLGs / risk
groups — srlgs and risk_groups are distinct namespaces, S6-7 fix), harvests
k-best placements over the groom_or_new frontier plus a
new_only fallback, and returns typed candidates (lever ip_reroute / optical_reroute
/ hybrid) with restored/shortfall capacity. Execution (validate_plan/commit_plan/
provision_lightpath) is Phase 7; this tool only enumerates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .network import NetworkModel
from .solvers import SolverStatus
from .multilayer_graph import NewLightpathRun
from .placement_common import _status
from .route_service import route_service


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


def compute_restoration(
    model: NetworkModel, qot, service_id: str, avoid: Optional[dict] = None,
) -> RestorationResult:
    """Enumerate recovery candidates for a service over survivors. `avoid` is
    `{assets?: [...], srlgs?: [...], risk_groups?: [...]}` (typically
    inject_failure's set). `srlgs` matches static SRLG ids, `risk_groups` matches
    dynamic RiskGroup ids only.

    Thin wrapper over route_service's single-candidate (unprotected) menu: the
    harvest, dedup, and materializability filtering all live there now. Ranking
    is re-sorted here on (shortfall_gbps, cost_vector["scalar"]) rather than
    route_service's plain scalar order, because restoration must still surface a
    FULLY-restoring candidate ahead of a cheaper-but-partial one.
    """
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
    status = _status(bool(candidates), any(c.shortfall_gbps == 0.0 for c in candidates))
    return RestorationResult(status, service_id, rs.demand_gbps, candidates)
