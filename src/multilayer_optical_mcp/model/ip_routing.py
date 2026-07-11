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
- `working_path` is the steady-state load-bearing path; `protection_path` is a
  dedicated 1:1 standby that reserves (but does not carry) its full demand and is
  activated by `simulate_ip_routing` the instant any working link goes down.
- The IP layer is undirected: one capacity scalar per link, either-orientation
  traversal in `is_contiguous_path` (S5-6 — per-direction optical asymmetry
  never reaches this layer; see NetworkModel.ip_link_capacity_gbps).
- `offered_load_per_link` is the working-only NOMINAL load; a pinned path may now
  reference a removed link (Phase 7 `remove_lightpath`/`remove_ip_link`), which
  the failover-aware `simulate_ip_routing` treats as a down link, never a KeyError.
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
    """Working-only NOMINAL load: each routed service's demand on every IP link in
    its pinned working_path, ignoring failures. Empty working_path => unrouted,
    carries no load. Missing (removed) links are skipped. Feeds the 1:1
    reservation-admission check and ip_topology; the failover-aware view lives in
    active_load_per_link / simulate_ip_routing."""
    load: Dict[str, float] = {link.id: 0.0 for link in model.list_ip_links()}
    for svc in model.list_services():
        for ip_id in svc.working_path:
            if ip_id in load:
                load[ip_id] += svc.demand_gbps
    return load


def reserved_capacity_per_link(model: NetworkModel) -> Dict[str, float]:
    """Σ protection-path demand reserved on each IP link. Dedicated 1:1: every
    protected service reserves its FULL demand on every link of its protection
    path, and reservations SUM across services sharing a link, so the reservation
    holds under any single-or-simultaneous failover. Feeds PROTECTION_OVERSUBSCRIBED."""
    reserved: Dict[str, float] = {link.id: 0.0 for link in model.list_ip_links()}
    for svc in model.list_services():
        for ip_id in svc.protection_path:
            if ip_id in reserved:
                reserved[ip_id] += svc.demand_gbps
    return reserved


def _link_status(model: NetworkModel, ip_id: str) -> str:
    """Three-state link status (C5-1, S5-4): "removed" (absent), "unknown" (present
    but no QoT recomputed yet — NOT down), "down" (capacity 0 / margin-negative),
    or "up" (capacity > 0). The unknown state is what stops a freshly-provisioned
    link from spuriously dropping its service before the first recompute."""
    try:
        cap = model.ip_link_capacity_gbps(ip_id)
    except KeyError:
        return "removed"
    except LookupError:
        return "unknown"
    return "up" if cap > 0.0 else "down"


def _path_usable(model: NetworkModel, path: Tuple[str, ...]) -> bool:
    """A path is ridable iff it is non-empty and every link is up OR unknown — an
    un-evaluated link is treated optimistically (unknown != down), so only a
    provably down/removed link forces failover."""
    return bool(path) and all(
        _link_status(model, ip) in ("up", "unknown") for ip in path)


def _active_path(model: NetworkModel, svc) -> Tuple[Optional[Tuple[str, ...]], str]:
    """The path a service currently rides under 1:1 auto-failover: working if
    ridable; else the reserved protection path if ridable; else (None, "none").
    Returns (path, "working" | "protection" | "none")."""
    if _path_usable(model, svc.working_path):
        return svc.working_path, "working"
    if _path_usable(model, svc.protection_path):
        return svc.protection_path, "protection"
    return None, "none"


def active_load_per_link(model: NetworkModel) -> Dict[str, float]:
    """Failover-aware load: each service contributes its demand to whichever path
    it currently rides (working if up, else reserved protection). A service with
    no usable path contributes nothing — it is dropped."""
    load: Dict[str, float] = {link.id: 0.0 for link in model.list_ip_links()}
    for svc in model.list_services():
        path, _which = _active_path(model, svc)
        if path is None:
            continue
        for ip_id in path:
            if ip_id in load:
                load[ip_id] += svc.demand_gbps
    return load


def _first_bad_link(model: NetworkModel, path: Tuple[str, ...]) -> Tuple[str, str]:
    """First link in `path` that is provably removed or down (an unknown link is
    NOT bad); ("", "none") if none. Attributes a drop to a concrete failed link."""
    for ip_id in path:
        status = _link_status(model, ip_id)
        if status in ("removed", "down"):
            return ip_id, status
    return "", "none"


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
    reason: str                    # "link_down" | "link_removed" | "unrouted"
    on_link: str                   # the failed link ("" for an unrouted service)


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
    restored_services: Tuple[str, ...] = ()   # riding reserved protection after a working failure


def simulate_ip_routing(model: NetworkModel) -> IPRoutingResult:
    """Failover-aware pure read: place each service on its ACTIVE path (working if
    up, else its reserved 1:1 protection), account utilization, and report
    congestion, drops, and services restored onto protection. Routes nothing;
    only pins the paths already declared on each service.

    S5-7: this function never consults `model.failed_assets()` directly — it
    trusts that a QoT recompute has already left the right sentinel in
    `_qot_state` (a bare `mark_failed` with no recompute still downs nothing
    here; `whatif.inject_failure` / `recompute_qot_under_loading` are the
    documented entry points that write the -inf sentinel). A link is "down" for
    failover purposes when it is removed (absent) or margin-negative — see
    `_link_is_up`.
    """
    load = active_load_per_link(model)
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
    dropped: List[DroppedService] = []
    restored: List[str] = []
    for svc in model.list_services():
        path, which = _active_path(model, svc)
        if path is None:
            if not svc.working_path:
                # A service with demand but no working path: its demand must not
                # silently vanish — it is lost traffic (Decision 6 conservation).
                dropped.append(DroppedService(svc.id, "unrouted", ""))
            else:
                bad_id, kind = _first_bad_link(model, svc.working_path)
                reason = "link_removed" if kind == "removed" else "link_down"
                dropped.append(DroppedService(svc.id, reason, bad_id))
        elif which == "protection":
            restored.append(svc.id)  # survived via reserved 1:1 failover
    return IPRoutingResult(
        utilizations=tuple(utils),
        congested_links=tuple(congested),
        down_links=tuple(down),
        dropped_services=tuple(dropped),
        overflow_gbps=overflow,
        restored_services=tuple(restored),
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
