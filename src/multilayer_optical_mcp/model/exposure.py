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


def lightpath_footprint(model: NetworkModel, oms_sequence: Tuple[str, ...]) -> FrozenSet[str]:
    """The full physical footprint of an OMS-sequence: ``oms_seq_asset_set`` plus
    the terminal drop ROADM the OMS elements omit (S8-3).

    This is the single crossing predicate shared by every failure-aware path —
    ``inject_failure`` (down a lightpath), ``recompute_qot_under_loading``
    (don't resurrect a downed lightpath, S8-1), and ``clear_failed`` (drop the
    sentinel only when nothing failed still crosses it, S8-6) — so the failed-set
    and the QoT sentinels can never disagree about what a lightpath crosses."""
    assets = set(oms_seq_asset_set(model, oms_sequence))
    term = terminal_roadm_id(model, oms_sequence)
    if term is not None:
        assets.add(term)
    return frozenset(assets)


def terminal_roadm_id(model: NetworkModel, oms_sequence: Tuple[str, ...]) -> "str | None":
    """The drop ROADM at the end of *oms_sequence*, or None.

    Each importer OMS's ``elements`` start at ``roadm_<src>`` but omit the drop
    ROADM (``roadm_<dst>``), so ``oms_seq_asset_set`` never includes it. Callers
    that must reason about the whole physical footprint — e.g. failing the
    destination ROADM (S8-3) — add this. Returns None when the path drops into a
    bare transceiver (no ``roadm_<dst>`` registered), as in legacy toy models.
    """
    if not oms_sequence:
        return None
    last = model.get_oms(oms_sequence[-1])
    candidate = f"roadm_{last.dst_node_id}"
    return candidate if model.has_roadm(candidate) else None


def path_endpoint_exclusions(
    model: NetworkModel, oms_sequence: Tuple[str, ...],
    *, endpoints: "tuple[str, str] | None" = None,
) -> "tuple[FrozenSet[str], FrozenSet[str]]":
    """A path's own ingress/egress nodes and their ROADM uids.

    Two paths between the same endpoints must share those endpoints; that sharing
    is mandated by the demand, not a routing choice, so it is excluded from every
    disjointness comparison (see ``path_basis_keys``). Uses the ``roadm_<node>``
    convention already relied on by ``terminal_roadm_id`` and ``_resolve_endpoint``;
    the ROADM set is empty for endpoints with no registered ROADM (legacy toy
    transceiver endpoints), which makes the exclusion a safe no-op there.

    ``endpoints``, when given as ``(src_node, dst_node)``, is used VERBATIM
    instead of inferring the path's ingress/egress from ``oms_sequence[0]``/
    ``[-1]``. Required for a Placement whose reused-lightpath and new-run
    segments are stored out of true physical traversal order (see
    ``multilayer_disjoint.placement_footprint_keys``), where positional
    inference silently names the wrong node as a "mandated endpoint".

    Endpoint *failure*-correlation ("do they die together?") is a different
    question and stays with ``get_exposure`` / ``service_asset_set``, not here.
    """
    if endpoints is not None:
        nodes = set(endpoints)
    else:
        if not oms_sequence:
            return frozenset(), frozenset()
        first = model.get_oms(oms_sequence[0])
        last = model.get_oms(oms_sequence[-1])
        nodes = {first.src_node_id, last.dst_node_id}
    roadms = {f"roadm_{n}" for n in nodes if model.has_roadm(f"roadm_{n}")}
    return frozenset(nodes), frozenset(roadms)


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
    {ip_link_ids} ∪ {lightpath_ids} ∪ {oms_ids} ∪ {fiber/amp/roadm uids} ∪
    {terminal drop ROADM}.

    Risk groups may be expressed at any of these layers (fiber-level for a
    storm hitting a span; oms-level for a cable cut; lightpath-level for a
    transponder failure). Including every layer in the asset set makes
    intersection layer-agnostic. Uses lightpath_footprint (not the narrower
    oms_seq_asset_set) so a risk group/asset query naming a lightpath's own
    destination ROADM is not silently missed (the importer convention omits
    the drop ROADM from oms.elements). A dangling ip-link id (removed
    lightpath/link, a documented valid state) contributes nothing.
    """
    assets: set[str] = set()
    for ip_id in ip_link_ids:
        lp_id = model.get_ip_link_lightpath_id(ip_id)
        if lp_id is None:
            continue
        assets.add(ip_id)
        assets.add(lp_id)
        assets |= lightpath_footprint(model, model.get_lightpath(lp_id).oms_sequence)
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
    endpoints: "tuple[str, str] | None" = None,
) -> FrozenSet[str]:
    """Project an OMS-sequence path into the set of namespaced comparison keys
    for a (basis, level). Two paths are disjoint under (basis, level) iff their
    key sets are disjoint.

    - basis `physical`: raw physical keys. `level=node` compares optical nodes;
      any other level compares spans (fiber/amp/roadm/oms uids).
    - basis `srlg` / `risk_group`: the group ids whose members intersect the
      path's physical asset set. `level` has NO EFFECT for these two bases —
      `add_srlg`/`add_risk_group` always use the same whole-group-membership
      reading (a group counts as shared if ANY of its member assets intersects
      the path's physical footprint, regardless of whether that overlap is a
      node or a span) no matter which of `node`/`link`/`srlg`/`risk_group` is
      passed as `level`. This is the coarsest/strictest reading: it never
      under-reports a correlation, it just doesn't offer the narrower
      node-only or span-only granularity that `level` implies for `physical`.
      True per-level SRLG/risk-group granularity (e.g. "only count a group
      membership that lands on a shared node, not any shared span") is not
      implemented; no caller currently depends on it (checked
      route_service.py, allocation.py, solvers.py, and the MCP tool callers —
      all either default `level` to "link" or pass it as a same-named
      convention, e.g. `basis="srlg", level="srlg"`, never expecting
      `level="node"` to narrow an `srlg`/`risk_group`-basis result).
    - basis `union`: union of physical (at the given level) + srlg + risk_group
      keys — disjoint under union means disjoint under all of them, subject to
      the srlg/risk_group level caveat above.
    """
    endpoint_nodes, endpoint_roadms = path_endpoint_exclusions(
        model, oms_sequence, endpoints=endpoints)
    # Exclude the path's own endpoints under EVERY basis: an endpoint shared by
    # both paths is mandated by the demand, not a routing correlation, so it must
    # not intersect for physical, srlg, risk_group, or union.
    #
    # Uses lightpath_footprint (not the narrower oms_seq_asset_set) so the
    # path's own TERMINAL drop ROADM -- omitted from oms.elements by the
    # importer convention (see lightpath_footprint's docstring) -- is present
    # to intersect against. endpoint_roadms already correctly names that
    # terminal ROADM when it IS this path's own true endpoint (subtracted
    # below), so a NON-endpoint terminal ROADM (e.g. one path's destination
    # that isn't the other path's, or an SRLG/risk-group asset naming it) now
    # correctly registers instead of silently never intersecting.
    phys = lightpath_footprint(model, oms_sequence) - endpoint_roadms
    keys: set[str] = set()

    def add_physical() -> None:
        # Node-disjointness must never be WEAKER than link-disjointness: two
        # vertex-disjoint paths are automatically edge-disjoint (standard
        # graph-theory implication), so level="node" includes the span-level
        # (phys) keys as a floor, plus the node-only interior-node refinement
        # on top -- making it a strictly harder (superset) condition, not an
        # independent, sometimes-weaker one. Without the phys floor, a
        # single-hop path's node-only set is always empty (its only touched
        # nodes ARE its own endpoints), so it would be trivially "disjoint"
        # from anything, including itself.
        keys.update(_PHYS + a for a in phys)
        if level == "node":
            nodes = oms_seq_node_set(model, oms_sequence) - endpoint_nodes
            keys.update(_NODE + n for n in nodes)

    def add_srlg() -> None:
        # `level` is intentionally not consulted here: whole-group membership
        # is the only reading implemented for basis="srlg" (see the docstring
        # above). Any intersection between the group's asset ids and the
        # path's physical footprint -- node or span -- counts.
        for g in model.list_srlgs():
            if set(g.asset_ids) & phys:
                keys.add(_SRLG + g.id)

    def add_risk_group() -> None:
        # Same caveat as add_srlg(): `level` is not consulted, whole-group
        # membership only.
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
