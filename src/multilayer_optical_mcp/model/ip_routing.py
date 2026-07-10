"""IP routing models and functions.

Coupling chokepoint: capacity is DERIVED from the bound lightpath's QoT-gated
mode (NetworkModel.ip_link_capacity_gbps), never stored — CLAUDE.md's
derived-capacity rule. `simulate_ip_routing` is a pure read: it never reroutes,
never mutates state, and (S5-4) never raises out of the MCP surface.

Stage 5 assumptions (recorded explicitly, from the inspection roadmap):
- Every IP link's lightpath is assumed to have a recorded QoT state; before the
  first `recompute_qot_under_loading`, `ip_link_capacity_gbps` raises
  LookupError and callers here treat that as capacity "unknown" (S5-4), never a
  crash.
- `working_path` is the only load-bearing path; `protection_path` is standby and
  contributes zero load.
- The IP layer is undirected: one capacity scalar per link, either-orientation
  traversal in `is_contiguous_path` (S5-6 — per-direction optical asymmetry
  never reaches this layer; see NetworkModel.ip_link_capacity_gbps).
- `offered_load_per_link`'s dict is keyed only by currently-existing IP links;
  pinned paths always reference live links (no `remove_ip_link` exists).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from .network import NetworkModel


@dataclass(frozen=True)
class GroomingMap:
    """Bidirectional mapping between services and lightpaths.

    Attributes:
        by_service: Map from service id to tuple of lightpath ids on its working
            path, IN PATH ORDER, WITH DUPLICATES if the path crosses one
            lightpath twice (a routing loop). S5-2: chosen over dedup so
            by_service stays a faithful trace of the path, consistent with how
            offered_load_per_link sums demand per link traversal (also not
            deduped). by_lightpath (the reverse map) DOES dedup service ids,
            since "does this service use this lightpath" is boolean.
        by_lightpath: Map from lightpath id to tuple of service ids using it (sorted).
    """

    by_service: Dict[str, Tuple[str, ...]]
    by_lightpath: Dict[str, Tuple[str, ...]]


def build_grooming_map(model: NetworkModel) -> GroomingMap:
    """Build a bidirectional grooming map from the network model.

    Derives which services (IP demands) ride which lightpaths by tracing each
    service's working_path (sequence of IP link ids) to the lightpaths those links
    are bound to.

    Args:
        model: The network model.

    Returns:
        GroomingMap with by_service and by_lightpath dictionaries.
    """
    by_service: Dict[str, Tuple[str, ...]] = {}
    rev: Dict[str, list] = {}

    for svc in model.list_services():
        # Get lightpath ids for each IP link in the service's working path
        lps = tuple(model.get_ip_link(ip).lightpath_id for ip in svc.working_path)
        by_service[svc.id] = lps

        # Build reverse mapping: lightpath -> list of service ids
        for lp in lps:
            rev.setdefault(lp, [])
            if svc.id not in rev[lp]:
                rev[lp].append(svc.id)

    # Convert reverse mapping to sorted tuples
    by_lightpath = {lp: tuple(sorted(svcs)) for lp, svcs in rev.items()}

    return GroomingMap(by_service=by_service, by_lightpath=by_lightpath)


def offered_load_per_link(model: NetworkModel) -> Dict[str, float]:
    """Sum each routed service's demand onto every IP link in its pinned
    working_path. Empty working_path => unrouted, carries no load."""
    load: Dict[str, float] = {link.id: 0.0 for link in model.list_ip_links()}
    for svc in model.list_services():
        for ip_id in svc.working_path:
            load[ip_id] += svc.demand_gbps
    return load


@dataclass(frozen=True)
class LinkUtilization:
    ip_link_id: str
    offered_gbps: float
    capacity_gbps: Optional[float]  # None when the bound lightpath has no recorded QoT
    utilization: Optional[float]    # offered/capacity; None when down or capacity unknown
    down: bool                      # capacity == 0 (margin-negative or torn down)


@dataclass(frozen=True)
class DroppedService:
    service_id: str
    reason: str                    # currently always "link_down"
    on_link: str


@dataclass(frozen=True)
class IPRoutingResult:
    """S5-9: `overflow_gbps` (excess on congested links) and `dropped_services`
    (losses on down links) are computed from disjoint link SETS — a link is
    either congested or down, never both — but one SERVICE can appear in both:
    once via a congested link's overflow and again, via a different link on its
    path, in dropped_services. Do not sum the two as "total lost traffic": that
    double-counts any such service."""
    utilizations: Tuple[LinkUtilization, ...]
    congested_links: Tuple[str, ...]      # utilization > 1, not down
    down_links: Tuple[str, ...]           # every down link (capacity 0), loaded or idle
    dropped_services: Tuple[DroppedService, ...]
    overflow_gbps: float                  # Σ max(0, offered-cap) over congested links


def simulate_ip_routing(model: NetworkModel) -> IPRoutingResult:
    """Pure read: account pinned working_path demand onto IP links and report
    utilization, congestion, and drops. Routes nothing.

    S5-7: this function never consults `model.failed_assets()` directly — it
    trusts that a QoT recompute has already left the right sentinel in
    `_qot_state`. Calling `model.mark_failed(...)` and never recomputing will
    NOT surface as a drop here (the stale feasible QoT is still on record);
    `whatif.inject_failure` writes the -inf sentinel immediately, which is why
    it is the documented entry point for failures. Since the C4-1 fix (S8-1),
    `recompute_qot_under_loading` also re-derives the sentinel from
    `model.failed_assets()` on every call regardless of whether
    `inject_failure` ever ran, so the gap is narrower than it once was: any
    recompute after a bare `mark_failed` self-heals it. But a bare
    `mark_failed` with no recompute at all still will not down anything here.
    """
    load = offered_load_per_link(model)
    utils: List[LinkUtilization] = []
    congested: List[str] = []
    down: List[str] = []
    overflow = 0.0
    for link in model.list_ip_links():
        offered = load[link.id]
        try:
            cap = model.ip_link_capacity_gbps(link.id)
        except LookupError:
            # S5-4: no QoT recorded yet (provisioned pre-recompute). Distinct
            # "unknown" state, not a crash and not down — a read tool never raises.
            utils.append(LinkUtilization(link.id, offered, None, None, False))
            continue
        is_down = cap == 0.0
        util = None if is_down else offered / cap
        utils.append(LinkUtilization(link.id, offered, cap, util, is_down))
        if is_down:
            down.append(link.id)  # S5-5: enumerate every down link, loaded or idle
        elif util is not None and util > 1.0:
            congested.append(link.id)
            overflow += offered - cap
    down_set = set(down)
    dropped: List[DroppedService] = []
    for svc in model.list_services():
        for ip_id in svc.working_path:
            if ip_id in down_set:
                dropped.append(DroppedService(svc.id, "link_down", ip_id))
                break  # service is fully lost once any link on it is down
    return IPRoutingResult(
        utilizations=tuple(utils),
        congested_links=tuple(congested),
        down_links=tuple(down),
        dropped_services=tuple(dropped),
        overflow_gbps=overflow,
    )


def is_contiguous_path(
    model: NetworkModel, src_router: str, dst_router: str,
    ip_path: Tuple[str, ...],
) -> bool:
    """True iff ip_path forms a connected walk src_router -> dst_router,
    traversing each IP link in either orientation. Empty path is contiguous
    only when src == dst."""
    cur = src_router
    for ip_id in ip_path:
        link = model.get_ip_link(ip_id)
        if link.a_router == cur:
            cur = link.z_router
        elif link.z_router == cur:
            cur = link.a_router
        else:
            return False
    return cur == dst_router


def affected_services(model: NetworkModel, asset_id: str) -> Tuple[str, ...]:
    """Reverse lookup: services whose working OR protection path touches
    asset_id, where asset_id may be an IP link, lightpath, OMS, or
    fiber/amp/roadm uid. Unknown asset -> empty tuple (not an error)."""
    from .exposure import _path_asset_set
    hits = []
    for svc in model.list_services():
        footprint = (_path_asset_set(model, svc.working_path)
                     | _path_asset_set(model, svc.protection_path))
        if asset_id in footprint:
            hits.append(svc.id)
    return tuple(sorted(hits))
