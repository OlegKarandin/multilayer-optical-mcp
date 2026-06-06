"""IP routing models and functions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

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
