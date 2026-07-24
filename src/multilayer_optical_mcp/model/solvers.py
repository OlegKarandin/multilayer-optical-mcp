"""Step-4 solvers: routing + disjointness over the optical OMS graph.

Deterministic, pure functions over the NetworkModel. Outcomes are typed
(`SolverStatus`); "no path" / "no disjoint pair" are typed results, never
raised exceptions (CLAUDE.md core design rule).

Routing is over an OMS graph: optical nodes are vertices, each OMS is an edge.
Parallel OMS between the same node pair are distinct routes, so candidate
enumeration walks node-simple-paths and expands the parallel-edge choices per
hop (node-level k-shortest alone would collapse parallel OMS into one route).

Stage 6 assumptions (recorded explicitly, from the inspection roadmap):
- The OMS routing graph is DIRECTED (S6-4): one edge per OMS in its travel
  direction; a bidirectional span is two independent directed OMS/edges, so
  compute_paths(A,B) can never return the B->A OMS and compute_disjoint_paths
  can never return the two directions of one span as a "disjoint" pair.
- Avoidance is layer-agnostic per-OMS-edge pruning, applied twice
  (build_oms_graph and re-threaded through _oms_between) — the design's key
  correctness property; it holds because both filter on the same `forbidden`
  set (see S6-9 below).
- `avoid.assets` intersects the OMS asset set (oms id + fiber/amp/roadm
  elements) PLUS both endpoint nodes, so naming a ROADM id in avoid.assets
  prunes every OMS through it, not just one fiber at that site.
- Enumeration is deterministic: `_oms_between` sorts by (length, id) when
  weight="length", else by id.
- Disjointness keys are namespaced (`phys:`/`node:`/`srlg:`/`rg:`) so
  basis="union" never collides two different kinds of key.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, List, Optional, Sequence, Tuple

import networkx as nx

from .network import NetworkModel
from .exposure import path_basis_keys, split_shared_keys, oms_seq_asset_set


class SolverStatus(str, Enum):
    SOLUTION = "solution"
    NO_SOLUTION = "no_solution"
    PARTIAL = "partial"


@dataclass(frozen=True)
class OmsPath:
    """A route through the optical layer: the optical-node sequence and the
    OMS-id sequence realising it."""
    node_sequence: Tuple[str, ...]
    oms_sequence: Tuple[str, ...]


@dataclass(frozen=True)
class RoutingResult:
    status: SolverStatus
    paths: Tuple[OmsPath, ...] = ()


@dataclass(frozen=True)
class DisjointnessResult:
    status: SolverStatus
    disjoint: bool
    basis: str
    level: str
    path_a: Optional[OmsPath] = None
    path_b: Optional[OmsPath] = None
    shared_assets: Tuple[str, ...] = ()
    shared_groups: Tuple[str, ...] = ()


# Cap on *distinct node paths* enumerated for disjoint-pair search — NOT raw
# emissions. Counting emissions let a single node path with many parallel OMS
# flood the window and starve topologically-distinct disjoint routes (false
# NO_SOLUTION). _DISJOINT_EMISSION_CAP is a generous safety bound on total
# candidates (parallels within the capped node paths) to keep the O(n²) pairwise
# scan tractable on pathologically parallel topologies.
_DISJOINT_CANDIDATE_CAP = 32
_DISJOINT_EMISSION_CAP = 1024


def build_oms_graph(model: NetworkModel, forbidden: frozenset = frozenset()) -> nx.MultiDiGraph:
    """Optical nodes as vertices, one *directed* edge per OMS src->dst (carrying
    its id). MultiDiGraph so parallel OMS between the same ordered node pair stay
    distinct AND the two directions of one bidirectional span are separate edges.
    Directed because an OMS carries traffic src->dst only: routing an A->B demand
    must not offer the B->A OMS of the same span (which would both misroute a
    lightpath and let the two directions of one physical span masquerade as a
    disjoint working/protection pair)."""
    g: nx.MultiDiGraph = nx.MultiDiGraph()
    for oms in model.list_oms():
        if oms.id in forbidden:
            continue
        g.add_node(oms.src_node_id)
        g.add_node(oms.dst_node_id)
        g.add_edge(oms.src_node_id, oms.dst_node_id, key=oms.id, oms_id=oms.id)
    return g


def oms_length_km(model: NetworkModel, oms_id: str) -> float:
    """Total fiber length (km) of an OMS — Σ of its constituent fibers' lengths.
    Non-fiber elements (amps, roadms) contribute zero."""
    total = 0.0
    for el in model.get_oms(oms_id).elements:
        try:
            total += model.get_fiber(el).length_km
        except KeyError:
            pass
    return total


def _avoid_sets(constraints: Optional[dict]) -> Tuple[frozenset, frozenset, frozenset]:
    """Extract (avoid_assets, avoid_srlgs, avoid_risk_groups) from a constraints
    dict. Missing/empty -> empty sets (no pruning).

    S6-7 fix (2026-07-24): srlgs and risk_groups are now two distinct namespaces
    (mirrors exposure.py's srlg:/rg: keys) instead of one risk_groups key matching
    both static SRLGs and dynamic RiskGroups — an id collision between the two no
    longer silently expands both."""
    avoid = (constraints or {}).get("avoid") or {}
    return (frozenset(avoid.get("assets", ())),
            frozenset(avoid.get("srlgs", ())),
            frozenset(avoid.get("risk_groups", ())))


def forbidden_oms(
    model: NetworkModel, avoid_assets: frozenset, avoid_srlgs: frozenset,
    avoid_risk_groups: frozenset,
) -> frozenset:
    """OMS ids to prune: an OMS is forbidden if any of its assets (own id,
    fiber/amp/roadm elements, or either endpoint node) is in avoid_assets, or if
    a named SRLG (avoid_srlgs) or RiskGroup (avoid_risk_groups) has a member
    intersecting the OMS's physical asset set. SRLGs and RiskGroups are searched
    separately (S6-7 fix) — an id collision between the two no longer double-matches."""
    if not avoid_assets and not avoid_srlgs and not avoid_risk_groups:
        return frozenset()
    group_members: set = set()
    for g in model.list_srlgs():
        if g.id in avoid_srlgs:
            group_members.update(g.asset_ids)
    for g in model.list_risk_groups():
        if g.id in avoid_risk_groups:
            group_members.update(g.asset_ids)
    bad: set = set()
    for oms in model.list_oms():
        phys = set(oms_seq_asset_set(model, (oms.id,)))
        phys.add(oms.src_node_id)
        phys.add(oms.dst_node_id)
        if (phys & avoid_assets) or (phys & group_members):
            bad.add(oms.id)
    return frozenset(bad)


def _oms_between(
    model: NetworkModel, u: str, v: str, *, by_length: bool = False,
    forbidden: frozenset = frozenset(),
) -> List[str]:
    """OMS ids carrying traffic u->v (directed: src_node_id==u, dst_node_id==v),
    ordered deterministically — by (length, id) when *by_length*, else by id.
    Direction-strict so an A->B hop never resolves to the reverse-direction OMS
    of the same span."""
    out = [oms.id for oms in model.list_oms()
           if oms.src_node_id == u and oms.dst_node_id == v and oms.id not in forbidden]
    if by_length:
        return sorted(out, key=lambda o: (oms_length_km(model, o), o))
    return sorted(out)


def _enumerate_oms_paths(
    model: NetworkModel, src: str, dst: str, k: int, weight: str = "hops",
    forbidden: frozenset = frozenset(), max_node_paths: Optional[int] = None,
) -> Iterator[OmsPath]:
    """Yield up to `k` OMS-sequence routes src->dst, shortest first, expanding
    parallel OMS per hop. `weight="hops"` (default) orders by segment count;
    `weight="length"` orders by total fiber km (the routing objective for RSA,
    since reachable SNR tracks length). When *max_node_paths* is set, stop after
    that many distinct node paths have been expanded (parallels within them do
    not count toward the limit) — so a highly-parallel earlier node path cannot
    starve topological diversity in the disjoint-pair search.

    S6-5: `weight="length"` is only APPROXIMATELY length-ordered, not a true
    k-shortest-by-km guarantee — the collapsed simple graph gives each hop the
    MINIMUM parallel-OMS length, so node paths are ranked by best-case
    parallel, and hop expansion then emits every parallel per hop in odometer
    order (not re-sorted by realized total length). `solve_rsa` relies on this
    ordering as a heuristic proxy for reachable SNR, not a certified guarantee;
    the first `k` results are not provably shortest-by-fiber-km."""
    g = build_oms_graph(model, forbidden)
    if src not in g or dst not in g or src == dst:
        return
    by_length = weight == "length"
    # Collapse parallels to a simple *directed* graph for node-simple-path
    # enumeration, then re-expand the OMS choices per hop. Directed so node paths
    # respect OMS travel direction. For length weighting, each simple edge carries
    # the *shortest* parallel OMS length between the ordered node pair.
    simple = nx.DiGraph()
    simple.add_nodes_from(g.nodes)
    for u, v in g.edges():
        if by_length:
            # S6-9: min() assumes _oms_between(u, v, forbidden=forbidden) is
            # non-empty for every edge g.edges() yields — true only because `g`
            # (from build_oms_graph) and _oms_between are filtered on the SAME
            # `forbidden` set. If the two filters ever diverge this raises
            # ValueError on an empty min() rather than silently misrouting;
            # that's intentional — a loud failure, not a defensive fallback.
            w = min(oms_length_km(model, o) for o in _oms_between(model, u, v, forbidden=forbidden))
            if simple.has_edge(u, v):
                simple[u][v]["weight"] = min(simple[u][v]["weight"], w)
            else:
                simple.add_edge(u, v, weight=w)
        else:
            simple.add_edge(u, v)
    emitted = 0
    try:
        node_paths = nx.shortest_simple_paths(
            simple, src, dst, weight="weight" if by_length else None)
    except nx.NetworkXNoPath:
        return
    node_paths_seen = 0
    for node_path in node_paths:
        if max_node_paths is not None and node_paths_seen >= max_node_paths:
            return
        node_paths_seen += 1
        hop_options = [_oms_between(model, u, v, by_length=by_length, forbidden=forbidden)
                       for u, v in zip(node_path, node_path[1:])]
        for combo in itertools.product(*hop_options):
            yield OmsPath(node_sequence=tuple(node_path), oms_sequence=tuple(combo))
            emitted += 1
            if emitted >= k:
                return


def compute_paths(
    model: NetworkModel, src: str, dst: str, k: int,
    constraints: Optional[dict] = None, weight: str = "hops",
) -> RoutingResult:
    """k-shortest OMS routes src->dst (`weight` ∈ {"hops", "length"}). No route
    -> typed NO_SOLUTION."""
    avoid_assets, avoid_srlgs, avoid_rgs = _avoid_sets(constraints)
    forbidden = forbidden_oms(model, avoid_assets, avoid_srlgs, avoid_rgs)
    paths = tuple(_enumerate_oms_paths(model, src, dst, k, weight=weight, forbidden=forbidden))
    if not paths:
        return RoutingResult(status=SolverStatus.NO_SOLUTION, paths=())
    return RoutingResult(status=SolverStatus.SOLUTION, paths=paths)


def _node_sequence(model: NetworkModel, oms_sequence: Sequence[str]) -> Tuple[str, ...]:
    """Best-effort node sequence for an OMS-sequence by chaining endpoints."""
    nodes: List[str] = []
    for oms_id in oms_sequence:
        oms = model.get_oms(oms_id)
        if not nodes:
            nodes = [oms.src_node_id, oms.dst_node_id]
        elif oms.src_node_id == nodes[-1]:
            nodes.append(oms.dst_node_id)
        elif oms.dst_node_id == nodes[-1]:
            nodes.append(oms.src_node_id)
        else:
            nodes.extend([oms.src_node_id, oms.dst_node_id])
    return tuple(nodes)


def check_disjointness(
    model: NetworkModel,
    path_a: Sequence[str],
    path_b: Sequence[str],
    basis: str,
    level: str,
) -> DisjointnessResult:
    """Audit whether two existing OMS-sequence paths are disjoint under a named
    basis/level. Returns shared assets/groups when they are not. Always a
    SOLUTION (the audit computed an answer); `disjoint` carries the verdict."""
    keys_a = path_basis_keys(model, tuple(path_a), basis=basis, level=level)
    keys_b = path_basis_keys(model, tuple(path_b), basis=basis, level=level)
    shared = keys_a & keys_b
    shared_assets, shared_groups = split_shared_keys(shared)
    return DisjointnessResult(
        status=SolverStatus.SOLUTION,
        disjoint=not shared,
        basis=basis,
        level=level,
        path_a=OmsPath(_node_sequence(model, path_a), tuple(path_a)),
        path_b=OmsPath(_node_sequence(model, path_b), tuple(path_b)),
        shared_assets=shared_assets,
        shared_groups=shared_groups,
    )


def compute_disjoint_paths(
    model: NetworkModel, src: str, dst: str,
    basis: str, level: str, best_effort: bool = False, weight: str = "hops",
    constraints: Optional[dict] = None,
) -> DisjointnessResult:
    """Find a disjoint pair src->dst under a basis/level. Returns the first
    fully-disjoint pair as SOLUTION; with best_effort=True returns the
    minimum-overlap pair as PARTIAL when no fully-disjoint pair exists; with
    best_effort=False and none disjoint, NO_SOLUTION. `weight` ∈ {"hops",
    "length"} orders candidate routes.

    S6-8: "minimum-overlap" (best_effort) minimizes the COUNT of shared
    namespaced keys (`len(shared)`), not physical severity — one shared SRLG
    (1 key) ranks better than two shared amps (2 keys) regardless of how many
    correlated physical assets the SRLG actually covers. Documented rather than
    weighted by asset count: the cap-32 candidate window (_DISJOINT_CANDIDATE_CAP)
    already makes an exact severity ranking unreliable, so a naive weighting
    would be false precision."""
    avoid_assets, avoid_srlgs, avoid_rgs = _avoid_sets(constraints)
    forbidden = forbidden_oms(model, avoid_assets, avoid_srlgs, avoid_rgs)
    cands = list(_enumerate_oms_paths(model, src, dst, _DISJOINT_EMISSION_CAP,
                                      weight=weight, forbidden=forbidden,
                                      max_node_paths=_DISJOINT_CANDIDATE_CAP))
    keyed = [(p, path_basis_keys(model, p.oms_sequence, basis=basis, level=level))
             for p in cands]

    best: Optional[Tuple[OmsPath, OmsPath, frozenset]] = None
    best_overlap = None
    for i in range(len(keyed)):
        for j in range(i + 1, len(keyed)):
            (pa, ka), (pb, kb) = keyed[i], keyed[j]
            shared = ka & kb
            if not shared:
                return DisjointnessResult(
                    status=SolverStatus.SOLUTION, disjoint=True,
                    basis=basis, level=level, path_a=pa, path_b=pb,
                )
            if best is None or len(shared) < best_overlap:
                best = (pa, pb, shared)
                best_overlap = len(shared)

    if best_effort and best is not None:
        pa, pb, shared = best
        shared_assets, shared_groups = split_shared_keys(shared)
        return DisjointnessResult(
            status=SolverStatus.PARTIAL, disjoint=False,
            basis=basis, level=level, path_a=pa, path_b=pb,
            shared_assets=shared_assets, shared_groups=shared_groups,
        )
    return DisjointnessResult(
        status=SolverStatus.NO_SOLUTION, disjoint=False,
        basis=basis, level=level,
    )
