from __future__ import annotations
from dataclasses import dataclass
from typing import FrozenSet, Tuple
from .network import NetworkModel


@dataclass(frozen=True)
class ExposureResult:
    """Result of intersecting a service's asset footprint with a risk group.

    `both_intersect` is the load-bearing signal: working AND protection both
    touch the group, so the pair that was disjoint at design time is now
    correlated under this partition (CLAUDE.md scenario 1).
    """
    service_id: str
    risk_group_id: str
    working_intersects: bool
    protection_intersects: bool
    both_intersect: bool
    working_intersection: Tuple[str, ...]
    protection_intersection: Tuple[str, ...]


def _path_asset_set(model: NetworkModel, ip_link_ids: Tuple[str, ...]) -> FrozenSet[str]:
    """Expand IP-link-id sequence to the full multi-layer asset set:
    {ip_link_ids} ∪ {lightpath_ids} ∪ {oms_ids} ∪ {fiber/amp/roadm uids}.

    Risk groups may be expressed at any of these layers (fiber-level for a
    storm hitting a span; oms-level for a cable cut; lightpath-level for a
    transponder failure). Including every layer in the asset set makes
    intersection layer-agnostic.
    """
    assets: set[str] = set()
    for ip_id in ip_link_ids:
        assets.add(ip_id)
        link = model.get_ip_link(ip_id)
        lp = model.get_lightpath(link.lightpath_id)
        assets.add(lp.id)
        for oms_id in lp.oms_sequence:
            assets.add(oms_id)
            oms = model.get_oms(oms_id)
            assets.update(oms.elements)
    return frozenset(assets)


def service_asset_set(
    model: NetworkModel, service_id: str, *, which: str,
) -> FrozenSet[str]:
    """Return the full asset footprint of a service's `working` or
    `protection` path. Raises KeyError on unknown service."""
    svc = model.get_service(service_id)
    if which == "working":
        return _path_asset_set(model, svc.working_path)
    if which == "protection":
        return _path_asset_set(model, svc.protection_path)
    raise ValueError(f"which must be 'working' or 'protection', got {which!r}")


def compute_exposure(
    model: NetworkModel, service_id: str, risk_group_id: str,
) -> ExposureResult:
    """Intersect a service's working+protection asset footprints with a
    risk group. Unknown asset ids in the risk group miss silently."""
    rg = model.get_risk_group(risk_group_id)
    rg_assets = frozenset(rg.asset_ids)
    working = service_asset_set(model, service_id, which="working")
    protection = service_asset_set(model, service_id, which="protection")
    w_hit = tuple(sorted(working & rg_assets))
    p_hit = tuple(sorted(protection & rg_assets))
    return ExposureResult(
        service_id=service_id,
        risk_group_id=risk_group_id,
        working_intersects=bool(w_hit),
        protection_intersects=bool(p_hit),
        both_intersect=bool(w_hit) and bool(p_hit),
        working_intersection=w_hit,
        protection_intersection=p_hit,
    )
