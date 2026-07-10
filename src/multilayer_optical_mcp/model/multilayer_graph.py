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

import itertools
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterator, List, Optional, Tuple

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


def _residual_gbps(model: NetworkModel, lp, load: Dict[str, float]) -> float:
    """Derived capacity of the lightpath's bound IP link(s) minus current load.
    A lightpath with no IP link bound yields its full mode rate (margin-gated).

    `load` is the offered-load-per-IP-link map, built ONCE by the caller and
    passed in — rebuilding it per lightpath is O(L·S) (S5-8/S7-8).

    Over multiple bound IP links we take the **min** residual (the bottleneck),
    not the max. A groom onto this lightpath rides one of its bound IP links, and
    at graph-build time we don't know which, so the honest headroom is the tightest
    link's: `max` would report the healthiest link and overstate capacity a
    saturated sibling link can't actually provide (a confident wrong number)."""
    ip_ids = model.ip_links_for_lightpath(lp.id)
    if not ip_ids:
        # no IP link bound: capacity is mode rate iff margin >= 0
        try:
            state = model.get_qot_state(lp.id)
        except LookupError:
            return 0.0
        return 0.0 if state.margin_db < 0 else model.modes.get(lp.mode_id).bitrate_gbps
    residual = float("inf")
    for ip_id in ip_ids:
        cap = model.ip_link_capacity_gbps(ip_id)   # 0.0 when margin<0
        residual = min(residual, cap - load.get(ip_id, 0.0))
    return residual


def build_layered_graph(
    model: NetworkModel,
    forbidden_assets: FrozenSet[str] = frozenset(),
    *,
    grid: SpectrumGrid | None = None,
) -> nx.MultiDiGraph:
    """Construct the layered auxiliary graph for the model's current loading.
    `forbidden_assets` prunes any OMS touching them (no WLE) and any lightpath
    crossing them (no LPE).

    A MultiDiGraph (not a plain DiGraph) so parallel OMS between the same ordered
    node pair stay distinct per wavelength: on a DiGraph the second WLE
    ``(WL,a,lam)->(WL,b,lam)`` overwrites the first, silently collapsing parallel
    fibers to one route per slot (S7-13). Mirrors the flat OMS solver's
    MultiDiGraph (S6-4). Parallel lightpaths on the same access hop stay distinct
    for the same reason."""
    from .ip_routing import offered_load_per_link
    grid = grid or SpectrumGrid.default()
    g: nx.MultiDiGraph = nx.MultiDiGraph()
    spectrum = build_spectrum_state(model, grid)
    # Build the offered-load map once (S5-8/S7-8): _residual_gbps used to rebuild
    # it per lightpath (O(L·S)); it's loading-state-wide, so hoist it out of the loop.
    load = offered_load_per_link(model)

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
        residual = _residual_gbps(model, lp, load)
        if residual <= 0.0:
            continue
        u, v = _lightpath_endpoints(model, lp)
        g.add_edge((ACCESS, u), (ACCESS, v), key=lp.id,
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
                # key=oms.id keeps each parallel OMS a distinct WLE edge on the
                # ordered (WL,a,lam)->(WL,b,lam) pair (the S7-13 fix); TxE/RxE use
                # a constant key so multiple OMS at the same node/slot collapse to
                # one launch/terminate edge rather than accumulating duplicates.
                g.add_edge((WL, a, lam), (WL, b, lam), key=oms.id,
                           kind="WLE", oms_id=oms.id, lam=lam, weight=_W_WLE)
                g.add_edge((ACCESS, a), (WL, a, lam), key="TxE",
                           kind="TxE", lam=lam, weight=_W_NEW_LP)
                g.add_edge((WL, b, lam), (ACCESS, b), key="RxE",
                           kind="RxE", lam=lam, weight=_W_RXE)
    return g


def lpe_edges(g: nx.MultiDiGraph) -> List[Tuple]:
    """All LPE edges as (u, v, data)."""
    return [(u, v, d) for u, v, d in g.edges(data=True) if d.get("kind") == "LPE"]


def wle_count_on_layer(g: nx.MultiDiGraph, oms_id: str, lam: int) -> int:
    """Number of WLE edges for an OMS on a given wavelength layer (0 or 2:
    one per direction)."""
    return sum(1 for _, _, d in g.edges(data=True)
               if d.get("kind") == "WLE" and d.get("oms_id") == oms_id and d.get("lam") == lam)


# ---------------------------------------------------------------------------
# place_demands — IGABAG k-best placement
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewLightpathRun:
    oms_sequence: Tuple[str, ...]
    lam: int
    mode_id: str
    gsnr_db: float
    bitrate_gbps: float
    # Travel direction of the run (the demand's direction). `oms_sequence` is in
    # physical-OMS order, which may be traversed in reverse for a return-direction
    # demand; (src_node, dst_node) records the actual endpoints so provisioning
    # does not derive a reversed lightpath from oms_sequence.
    src_node: str = ""
    dst_node: str = ""


@dataclass(frozen=True)
class Placement:
    reused_lightpaths: Tuple[str, ...]
    new_lightpaths: Tuple[NewLightpathRun, ...]
    restored_gbps: float
    shortfall_gbps: float


# Enumeration budget: walk up to _PATH_BUDGET simple paths per policy, keeping
# up to _DEFAULT_K distinct feasible placements (the cost-ordered frontier).
_PATH_BUDGET = 64
_DEFAULT_K = 8

# Generous safety cap on RAW node paths drawn from shortest_simple_paths. The
# _PATH_BUDGET / _DEFAULT_K guards count DISTINCT routes / accepted placements, so
# on a topology with few distinct routes but a wide grid neither fires and the
# generator drains to exhaustion — thousands of lambda-mixing simple paths (new
# lightpaths regenerated across slots at an access node), Yen's algorithm churning
# on each. This bounds that work while staying far above the lambda-variant count
# that would otherwise starve a strictly-more-expensive distinct route (the S7-6
# guard): a full C-band's worth of slots is < 128, so 1024 clears ~8 cheaper
# distinct routes' variants before cutting off.
_RAW_PATH_CAP = 1024


def _policy_graph(g: nx.MultiDiGraph, policy: str) -> nx.MultiDiGraph:
    """Restrict the graph to a lever:
      groom_only   - drop TxE edges: reuse existing lightpaths only (no new).
      new_only     - drop LPE edges: force fresh lightpaths (no reuse).
      groom_or_new - full graph (grooming wins on weight when feasible)."""
    if policy == "groom_or_new":
        return g
    if policy not in ("groom_only", "new_only"):
        raise ValueError(f"unknown policy {policy!r}")
    drop_kind = "TxE" if policy == "groom_only" else "LPE"
    h = g.copy()
    h.remove_edges_from([(u, v, key) for u, v, key, d in h.edges(keys=True, data=True)
                         if d.get("kind") == drop_kind])
    return h


def _collapse_to_simple(h: nx.MultiDiGraph) -> nx.DiGraph:
    """Collapse parallel edges to a simple DiGraph for node-simple-path
    enumeration (`nx.shortest_simple_paths` is not implemented for multigraphs),
    each simple edge carrying the MIN parallel weight so the ordering matches the
    cheapest realisation of the hop. The per-hop parallel choices are re-expanded
    over the MultiDiGraph afterwards (`_parse_paths`) — mirrors the flat solver's
    collapse-then-expand (S6-4)."""
    simple = nx.DiGraph()
    simple.add_nodes_from(h.nodes)
    for u, v, d in h.edges(data=True):
        w = d.get("weight", 1.0)
        if simple.has_edge(u, v):
            simple[u][v]["weight"] = min(simple[u][v]["weight"], w)
        else:
            simple.add_edge(u, v, weight=w)
    return simple


def _parse_paths(
    g: nx.MultiDiGraph, path: List,
) -> Iterator[Tuple[List[str], List[Tuple[Tuple[str, ...], int, str, str]]]]:
    """Expand an access->access *vertex* path into every concrete
    (reused_lightpath_ids, new_runs) it realises, choosing among parallel edges
    per hop. On a MultiDiGraph a single node path may correspond to several routes
    when parallel OMS (or parallel lightpaths) share an ordered vertex pair
    (S7-13) — `nx.shortest_simple_paths` yields node paths only, so the per-hop
    choice is re-expanded here (mirrors the flat solver's `itertools.product`).

    Each new_run is (oms_sequence, lam, src_node, dst_node); the travel endpoints
    come from the WL-vertex node components ((WL, node, lam)), so a return-direction
    run over a physically-forward OMS records its true direction rather than the
    OMS's physical orientation."""
    hops = list(zip(path, path[1:]))
    # per hop: the list of parallel edge-data dicts (MultiDiGraph get_edge_data
    # returns {key: data}); a plain node path collapses these into one choice.
    per_hop = [list(g.get_edge_data(a, b).values()) for a, b in hops]
    for combo in itertools.product(*per_hop):
        reused: List[str] = []
        new_runs: List[Tuple[Tuple[str, ...], int, str, str]] = []
        cur_oms: List[str] = []
        cur_lam: Optional[int] = None
        cur_src: Optional[str] = None
        cur_dst: Optional[str] = None
        for (a, b), d in zip(hops, combo):
            kind = d.get("kind")
            if kind == "LPE":
                reused.append(d["lightpath_id"])
            elif kind == "WLE":
                cur_oms.append(d["oms_id"])
                cur_lam = d["lam"]
                if cur_src is None:
                    cur_src = a[1]      # from-node of the first hop in this run
                cur_dst = b[1]          # to-node, advanced each hop
            elif kind == "RxE":
                if cur_oms:
                    new_runs.append((tuple(cur_oms), cur_lam, cur_src, cur_dst))
                    cur_oms, cur_lam, cur_src, cur_dst = [], None, None, None
            # TxE: entry into a wl layer; nothing to record
        yield reused, new_runs


def _bottleneck_residual(g: nx.MultiDiGraph, reused: List[str]) -> float:
    """Min residual_gbps across the reused LPE edges (inf if none reused)."""
    if not reused:
        return float("inf")
    by_lp = {d["lightpath_id"]: d["residual_gbps"]
             for _, _, d in g.edges(data=True) if d.get("kind") == "LPE"}
    return min(by_lp[lp] for lp in reused)


def place_demands(
    model: NetworkModel, g: nx.MultiDiGraph, qot, *,
    src: str, dst: str, demand_gbps: float, policy: str,
    k: int = _DEFAULT_K, grid: Optional[SpectrumGrid] = None,
) -> List[Placement]:
    """IGABAG for one demand, returning up to `k` DISTINCT feasible placements
    (the cost-ordered frontier under the policy), each possibly degraded. A
    placement may reuse existing lightpaths (LPE), light new ones (TxE->WLEs->
    RxE), or BOTH (a hybrid). Empty list when no feasible path exists."""
    from .allocation import _build_loading, _best_feasible_mode
    grid = grid or SpectrumGrid.default()
    h = _policy_graph(g, policy)
    simple = _collapse_to_simple(h)
    s, t = (ACCESS, src), (ACCESS, dst)
    if s not in simple or t not in simple or not nx.has_path(simple, s, t):
        return []
    spectrum = build_spectrum_state(model, grid)
    ref_mode = model.modes.list()[0].id
    out: List[Placement] = []
    seen: set = set()
    examined = 0     # DISTINCT routes examined (budget counter, not raw emissions)
    raw_paths = 0    # RAW node paths drawn (safety valve against generator drain)
    budget_hit = False
    for path in nx.shortest_simple_paths(simple, s, t, weight="weight"):
        if len(out) >= k or budget_hit or raw_paths >= _RAW_PATH_CAP:
            break
        raw_paths += 1
        # One node path may realise several routes when parallel OMS/lightpaths
        # share an ordered vertex pair (S7-13); expand the per-hop choices.
        for reused, new_runs in _parse_paths(h, path):
            if len(out) >= k:
                break
            # Deduplicate by structural route (reused LP ids + new OMS sequences),
            # ignoring wavelength slot: same OMS sequence on lam=0 and lam=1 is the
            # same route option, just a different channel assignment. Collapsing
            # them keeps the k-best frontier meaningful (diverse routes/groom
            # combos) instead of filling it with the same plan on every free slot.
            key = (tuple(reused), tuple(oms_seq for oms_seq, _, _, _ in new_runs))
            if key in seen:
                continue     # a lambda-variant of an already-seen route: does NOT
                             # advance the budget, so a route with many free slots
                             # can't starve structurally distinct routes.
            seen.add(key)
            examined += 1
            if examined > _PATH_BUDGET:
                budget_hit = True
                break
            realized: List[NewLightpathRun] = []
            feasible = True
            new_cap = float("inf")
            for oms_seq, lam, run_src, run_dst in new_runs:
                loading = _build_loading(grid, spectrum, oms_seq, lam, ref_mode)
                mode, gsnr = _best_feasible_mode(model, qot, oms_seq, loading, ref_mode)
                if mode is None:
                    feasible = False
                    break
                realized.append(NewLightpathRun(oms_seq, lam, mode.id, gsnr,
                                                 mode.bitrate_gbps,
                                                 src_node=run_src, dst_node=run_dst))
                new_cap = min(new_cap, mode.bitrate_gbps)
            if not feasible:
                continue
            groom_cap = _bottleneck_residual(g, reused)
            restored = min(demand_gbps, groom_cap, new_cap)
            if restored <= 0.0:
                continue
            out.append(Placement(
                reused_lightpaths=tuple(reused),
                new_lightpaths=tuple(realized),
                restored_gbps=restored,
                shortfall_gbps=max(0.0, demand_gbps - restored),
            ))
    return out
