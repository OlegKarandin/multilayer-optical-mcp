# src/multilayer_optical_mcp/model/multilayer_graph.py
"""Layered IP+optical auxiliary graph (Zhu/Mukherjee model, per-wavelength
layers, no wavelength conversion) + IGABAG single-demand placement.

Vertices:
  (ACCESS, node)        access/IP layer port for an optical node
  (WL, node, lam)       wavelength-layer port for node on slot `lam`

Edges (directed; every edge carries a 'weight'):
  LPE  access(u) -> access(v)   one per existing lightpath u->v.
        Carries lightpath_id + residual_gbps; absent when margin<0, residual 0,
        or the lightpath crosses a forbidden asset. Low weight (reuse).
  WLE  (WL,u,lam) -> (WL,v,lam)  one per free slot `lam` on an OMS u->v (both
        directions); carries oms_id + lam. Low weight.
  TxE  access(u) -> (WL,u,lam)   originate a new lightpath on slot lam. MODERATE
        weight (a few groom-hops' worth) so new segments are discouraged but still
        reachable by k-shortest within budget — letting hybrids interleave into
        the frontier instead of ranking behind every pure-groom path.
  RxE  (WL,v,lam) -> access(v)   terminate a new lightpath. Zero weight.

A path access(src) -> access(dst) that stays on existing lightpaths uses only
LPE edges (grooming). A path that dips via TxE -> WLEs (one lam) -> RxE realizes
a new lightpath on that wavelength. No CvtE: wavelength continuity is structural.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List, Tuple

import networkx as nx

from .network import NetworkModel
from .spectrum import SpectrumGrid, build_spectrum_state
from .exposure import oms_seq_asset_set

ACCESS = "access"
WL = "wl"

# Edge weights shape k-shortest DISCOVERY only (final ranking uses cost_facets).
# New lightpaths are discouraged but reachable, so hybrids/new candidates
# interleave into the frontier rather than ranking behind every pure-groom path
# (which a 1000x penalty would cause). Tunable.
_W_LPE = 1.0       # reuse an existing lightpath (one virtual hop)
_W_WLE = 0.1       # traverse one OMS on a new lightpath's wavelength
_W_NEW_LP = 5.0    # originate a new lightpath (TxE): a few groom-hops' worth
_W_RXE = 0.0


def _lightpath_endpoints(model: NetworkModel, lp) -> Tuple[str, str]:
    """(src_node, dst_node) of a lightpath from its first/last OMS endpoints."""
    first = model.get_oms(lp.oms_sequence[0])
    last = model.get_oms(lp.oms_sequence[-1])
    return first.src_node_id, last.dst_node_id


def _lightpath_forbidden(model: NetworkModel, lp, forbidden_assets: FrozenSet[str]) -> bool:
    if not forbidden_assets:
        return False
    assets = set()
    for oms_id in lp.oms_sequence:
        assets |= oms_seq_asset_set(model, (oms_id,))
        oms = model.get_oms(oms_id)
        assets.add(oms.src_node_id)
        assets.add(oms.dst_node_id)
    return bool(assets & forbidden_assets)


def _residual_gbps(model: NetworkModel, lp) -> float:
    """Derived capacity of the lightpath's bound IP link(s) minus current load.
    A lightpath with no IP link bound yields its full mode rate (margin-gated)."""
    from .ip_routing import offered_load_per_link
    ip_ids = model.ip_links_for_lightpath(lp.id)
    if not ip_ids:
        # no IP link bound: capacity is mode rate iff margin >= 0
        try:
            state = model.get_qot_state(lp.id)
        except LookupError:
            return 0.0
        return 0.0 if state.margin_db < 0 else model.modes.get(lp.mode_id).bitrate_gbps
    load = offered_load_per_link(model)
    residual = 0.0
    for ip_id in ip_ids:
        cap = model.ip_link_capacity_gbps(ip_id)   # 0.0 when margin<0
        residual = max(residual, cap - load.get(ip_id, 0.0))
    return residual


def build_layered_graph(
    model: NetworkModel,
    forbidden_assets: FrozenSet[str] = frozenset(),
    *,
    grid: SpectrumGrid | None = None,
) -> nx.DiGraph:
    """Construct the layered auxiliary graph for the model's current loading.
    `forbidden_assets` prunes any OMS touching them (no WLE) and any lightpath
    crossing them (no LPE)."""
    grid = grid or SpectrumGrid.default()
    g = nx.DiGraph()
    spectrum = build_spectrum_state(model, grid)

    # forbidden OMS: any OMS whose asset set / endpoints intersect forbidden_assets
    def _oms_forbidden(oms) -> bool:
        if not forbidden_assets:
            return False
        phys = set(oms_seq_asset_set(model, (oms.id,)))
        phys.add(oms.src_node_id)
        phys.add(oms.dst_node_id)
        return bool(phys & forbidden_assets)

    # access vertices for every optical node that appears on an OMS endpoint
    for oms in model.list_oms():
        g.add_node((ACCESS, oms.src_node_id))
        g.add_node((ACCESS, oms.dst_node_id))

    # LPE edges: existing lightpaths
    for lp in model.list_lightpaths():
        if _lightpath_forbidden(model, lp, forbidden_assets):
            continue
        residual = _residual_gbps(model, lp)
        if residual <= 0.0:
            continue
        u, v = _lightpath_endpoints(model, lp)
        g.add_edge((ACCESS, u), (ACCESS, v),
                   kind="LPE", lightpath_id=lp.id, residual_gbps=residual,
                   weight=_W_LPE)

    # WLE + TxE/RxE per wavelength layer
    for oms in model.list_oms():
        if _oms_forbidden(oms):
            continue
        occ = spectrum.get(oms.id, 0)
        u, v = oms.src_node_id, oms.dst_node_id
        for lam in range(grid.num_slots):
            if (occ >> lam) & 1:
                continue   # slot lit -> no WLE
            for a, b in ((u, v), (v, u)):
                g.add_edge((WL, a, lam), (WL, b, lam),
                           kind="WLE", oms_id=oms.id, lam=lam, weight=_W_WLE)
                # TxE / RxE tie the wavelength layer to access at each endpoint
                g.add_edge((ACCESS, a), (WL, a, lam), kind="TxE", lam=lam, weight=_W_NEW_LP)
                g.add_edge((WL, b, lam), (ACCESS, b), kind="RxE", lam=lam, weight=_W_RXE)
    return g


def lpe_edges(g: nx.DiGraph) -> List[Tuple]:
    """All LPE edges as (u, v, data)."""
    return [(u, v, d) for u, v, d in g.edges(data=True) if d.get("kind") == "LPE"]


def wle_count_on_layer(g: nx.DiGraph, oms_id: str, lam: int) -> int:
    """Number of WLE edges for an OMS on a given wavelength layer (0 or 2:
    one per direction)."""
    return sum(1 for _, _, d in g.edges(data=True)
               if d.get("kind") == "WLE" and d.get("oms_id") == oms_id and d.get("lam") == lam)
