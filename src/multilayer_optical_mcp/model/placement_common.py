"""Shared helpers used by route_service, restoration, and allocation: the
avoid-set -> forbidden-assets translation, the lever classifier for a
Placement, and the three-way SOLUTION/PARTIAL/NO_SOLUTION status rule.

Kept in a neutral module that imports none of route_service/restoration/
allocation, so those three don't have to import each other for shared logic.
Previously `_forbidden_assets`/`_lever` lived in restoration.py despite
restoration.py never calling either itself -- they existed there only for
route_service.py to import, which forced restoration.py to reach back into
route_service.py through a deferred, function-local import to dodge the
resulting cycle (docs/2026-07-19-open-todos.md #4, "layering inversion").
"""
from __future__ import annotations

from typing import FrozenSet, Optional

from .network import NetworkModel
from .multilayer_graph import Placement, place_demands
from .solvers import SolverStatus
from .spectrum import FillPolicy, SpectrumGrid


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


def _status(has_any: bool, fully_satisfied: bool) -> SolverStatus:
    """The SOLUTION/PARTIAL/NO_SOLUTION trichotomy shared by allocation,
    route_service, and restoration. Order matters: check `fully_satisfied`
    BEFORE `has_any`, so a caller whose "fully satisfied" bit is vacuously
    true on empty input (allocation: 0 unplaced out of 0 demands) reads as
    SOLUTION rather than NO_SOLUTION -- matching allocation._status's
    original check order. A caller whose "fully satisfied" bit is itself
    `any(...)` over an empty candidate list (route_service, restoration) gets
    False in both positions on empty input either way, so it still falls
    through to NO_SOLUTION. Callers compute `has_any`/`fully_satisfied` from
    their own data shape (placed counts, shortfall lists, ...)."""
    if fully_satisfied:
        return SolverStatus.SOLUTION
    if not has_any:
        return SolverStatus.NO_SOLUTION
    return SolverStatus.PARTIAL


def _harvest_placements(
    model, qot, g, src, dst, demand_gbps, k,
    fill_policy: FillPolicy = FillPolicy.FULL,
    grid: Optional[SpectrumGrid] = None,
) -> list:
    """groom_or_new + new_only frontiers over *g*, deduped on the lambda-free
    route identity (reused lightpath ids + new runs' oms_sequences) so a
    placement re-surfacing under both policies counts once. Shared by
    route_service.route_service (and, through it, restoration) and
    allocation's packer -- previously two independently drifting copies
    (docs/2026-07-19-open-todos.md #4). `grid`, when passed, must be the SAME
    SpectrumGrid instance used to build *g* (S7-12 fix) — see the callers in
    route_service.py / allocation.py."""
    out: list = []
    seen: set = set()
    for policy in ("groom_or_new", "new_only"):
        for p in place_demands(model, g, qot, src=src, dst=dst,
                               demand_gbps=demand_gbps, policy=policy, k=k,
                               fill_policy=fill_policy, grid=grid):
            key = (p.reused_lightpaths, tuple(r.oms_sequence for r in p.new_lightpaths))
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out
