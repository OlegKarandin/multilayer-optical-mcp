"""IP routing models and functions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .network import NetworkModel


@dataclass(frozen=True)
class GroomingMap:
    """Bidirectional mapping between services and lightpaths.

    Attributes:
        by_service: Map from service id to tuple of lightpath ids on its working path.
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
    capacity_gbps: float
    utilization: Optional[float]   # offered/capacity; None when the link is down
    down: bool                     # capacity == 0 (margin-negative or torn down)


@dataclass(frozen=True)
class DroppedService:
    service_id: str
    reason: str                    # currently always "link_down"
    on_link: str


@dataclass(frozen=True)
class IPRoutingResult:
    utilizations: Tuple[LinkUtilization, ...]
    congested_links: Tuple[str, ...]      # utilization > 1, not down
    down_links: Tuple[str, ...]           # capacity 0 with load > 0
    dropped_services: Tuple[DroppedService, ...]
    overflow_gbps: float                  # Σ max(0, offered-cap) over congested links


def simulate_ip_routing(model: NetworkModel) -> IPRoutingResult:
    """Pure read: account pinned working_path demand onto IP links and report
    utilization, congestion, and drops. Routes nothing."""
    load = offered_load_per_link(model)
    utils: List[LinkUtilization] = []
    congested: List[str] = []
    down: List[str] = []
    overflow = 0.0
    for link in model.list_ip_links():
        offered = load[link.id]
        cap = model.ip_link_capacity_gbps(link.id)
        is_down = cap == 0.0
        util = None if is_down else offered / cap
        utils.append(LinkUtilization(link.id, offered, cap, util, is_down))
        if is_down:
            if offered > 0.0:
                down.append(link.id)
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
