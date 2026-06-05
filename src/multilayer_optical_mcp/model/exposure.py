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


def oms_seq_asset_set(model: NetworkModel, oms_sequence: Tuple[str, ...]) -> FrozenSet[str]:
    """Expand an OMS-id sequence to {oms_ids} ∪ {fiber/amp/roadm uids}. Shared
    by IP-link expansion (below) and the routing/disjointness solvers, which
    work with OMS-sequences directly."""
    assets: set[str] = set()
    for oms_id in oms_sequence:
        assets.add(oms_id)
        oms = model.get_oms(oms_id)
        assets.update(oms.elements)
    return frozenset(assets)


def oms_seq_node_set(model: NetworkModel, oms_sequence: Tuple[str, ...]) -> FrozenSet[str]:
    """The optical node ids an OMS-sequence touches (each OMS endpoint). Used
    for node-level disjointness."""
    nodes: set[str] = set()
    for oms_id in oms_sequence:
        oms = model.get_oms(oms_id)
        nodes.add(oms.src_node_id)
        nodes.add(oms.dst_node_id)
    return frozenset(nodes)


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
        assets |= oms_seq_asset_set(model, lp.oms_sequence)
    return frozenset(assets)


# Internal key namespaces so physical assets, SRLG ids, and risk-group ids
# never collide under the `union` basis.
_PHYS = "phys:"
_NODE = "node:"
_SRLG = "srlg:"
_RG = "rg:"


def path_basis_keys(
    model: NetworkModel,
    oms_sequence: Tuple[str, ...],
    *,
    basis: str,
    level: str,
) -> FrozenSet[str]:
    """Project an OMS-sequence path into the set of namespaced comparison keys
    for a (basis, level). Two paths are disjoint under (basis, level) iff their
    key sets are disjoint.

    - basis `physical`: raw physical keys. `level=node` compares optical nodes;
      any other level compares spans (fiber/amp/roadm/oms uids).
    - basis `srlg` / `risk_group`: the group ids whose members intersect the
      path's physical asset set.
    - basis `union`: union of physical (at the given level) + srlg + risk_group
      keys — disjoint under union means disjoint under all of them.
    """
    phys = oms_seq_asset_set(model, oms_sequence)
    keys: set[str] = set()

    def add_physical() -> None:
        if level == "node":
            keys.update(_NODE + n for n in oms_seq_node_set(model, oms_sequence))
        else:
            keys.update(_PHYS + a for a in phys)

    def add_srlg() -> None:
        for g in model.list_srlgs():
            if set(g.asset_ids) & phys:
                keys.add(_SRLG + g.id)

    def add_risk_group() -> None:
        for g in model.list_risk_groups():
            if set(g.asset_ids) & phys:
                keys.add(_RG + g.id)

    if basis == "physical":
        add_physical()
    elif basis == "srlg":
        add_srlg()
    elif basis == "risk_group":
        add_risk_group()
    elif basis == "union":
        add_physical()
        add_srlg()
        add_risk_group()
    else:
        raise ValueError(f"unknown basis {basis!r}")
    return frozenset(keys)


def split_shared_keys(keys: FrozenSet[str]) -> tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Split namespaced keys into (shared_assets, shared_groups), stripping the
    namespace prefixes, for structured reporting."""
    assets: list[str] = []
    groups: list[str] = []
    for k in keys:
        if k.startswith(_PHYS):
            assets.append(k[len(_PHYS):])
        elif k.startswith(_NODE):
            assets.append(k[len(_NODE):])
        elif k.startswith(_SRLG):
            groups.append(k[len(_SRLG):])
        elif k.startswith(_RG):
            groups.append(k[len(_RG):])
    return tuple(sorted(assets)), tuple(sorted(groups))


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
