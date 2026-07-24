# src/multilayer_optical_mcp/model/route_service.py
"""Service-level routing/restoration on the layered graph (menu, no-consume).

avoid=None -> first-time routing (empty net -> all-new candidates).
avoid={assets?, risk_groups?} -> restoration over survivors.
protected=False -> up to k single candidates; protected=True -> disjoint-pair menu.
Read-only: every score is computed on a throwaway clone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .network import NetworkModel
from .solvers import SolverStatus
from .multilayer_graph import build_layered_graph, NewLightpathRun
from .placement_common import _forbidden_assets, _lever, _status, _harvest_placements
from .multilayer_disjoint import disjoint_pairs
from .objective import score_candidate, score_pair, placement_materializable
from .spectrum import SpectrumGrid


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


def route_service(model: NetworkModel, qot, service_id: str, *, protected: bool = False,
                  basis: str = "physical", level: str = "link", best_effort: bool = False,
                  avoid: Optional[dict] = None, weights: Optional[dict] = None,
                  k: int = 8, top_n: int = 5) -> RouteServiceResult:
    """`weights` here is PER-COST-TERM WEIGHTS passed through to
    evaluate_objective's 7-term objective scalar (e.g. `{"transponders": 2.0}`);
    it is not a per-demand priority (that's solve_allocation's `weights`, a
    different meaning of the same parameter name)."""
    svc = model.get_service(service_id)
    src = model.get_router(svc.src_router).site
    dst = model.get_router(svc.dst_router).site
    grid = SpectrumGrid.default()
    g = build_layered_graph(model, forbidden_assets=_forbidden_assets(model, avoid), grid=grid)
    placements = _harvest_placements(model, qot, g, src, dst, svc.demand_gbps, k, grid=grid)
    # A placement whose new run terminates at a router-less optical node cannot be
    # bound to an IP link (score_candidate/score_pair would KeyError provisioning
    # it). Such a placement is an INFEASIBLE service-routing candidate: exclude it
    # so the typed contract holds (empty -> NO_SOLUTION via _status/empty pairs),
    # never crash. build_layered_graph creates TxE/RxE at EVERY optical node, so a
    # split placement through a pass-through ROADM is a normal, reachable case.
    placements = [p for p in placements if placement_materializable(model, p)]

    if not protected:
        cands = [RouteServiceCandidate(
                    _lever(p), p.reused_lightpaths, p.new_lightpaths,
                    p.restored_gbps, p.shortfall_gbps,
                    _vec(score_candidate(model, p, svc, weights)))
                 for p in placements]
        cands.sort(key=lambda c: c.cost_vector["scalar"])
        status = _status(bool(cands), any(c.shortfall_gbps == 0.0 for c in cands))
        return RouteServiceResult(status, service_id, svc.demand_gbps, False,
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
