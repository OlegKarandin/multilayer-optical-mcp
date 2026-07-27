"""Disjointness over multilayer Placements.

A Placement's physical footprint is the union of the OMS its reused lightpaths
traverse AND the OMS its new runs light. Flattening reused lightpaths to their
oms_sequence (never treating a lightpath id as opaque) is the load-bearing step:
two different lightpaths sharing a fiber must read as correlated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .network import NetworkModel
from .multilayer_graph import Placement
from .exposure import path_basis_keys, split_shared_keys


def placement_footprint_keys(model: NetworkModel, placement: Placement, *,
                             basis: str, level: str,
                             endpoints: "tuple[str, str] | None" = None,
                             ) -> frozenset[str]:
    """`endpoints`, when given as (src_node, dst_node), is passed through to
    path_basis_keys verbatim rather than letting it infer the path's mandated
    endpoints positionally from the flattened oms_sequence -- Placement stores
    reused_lightpaths and new_lightpaths as separate tuples with no shared
    traversal-order information, so for a hybrid placement (both non-empty)
    the concatenation below is NOT reliably in true physical order, and
    positional inference can silently exclude the wrong node."""
    oms_seq: List[str] = []
    for lp_id in placement.reused_lightpaths:
        oms_seq += list(model.get_lightpath(lp_id).oms_sequence)
    for run in placement.new_lightpaths:
        oms_seq += list(run.oms_sequence)
    return path_basis_keys(model, tuple(oms_seq), basis=basis, level=level,
                           endpoints=endpoints)


@dataclass(frozen=True)
class PlacementPair:
    working: Placement
    protection: Placement
    disjoint: bool
    shared_assets: Tuple[str, ...]
    shared_groups: Tuple[str, ...]
    overlap: int                 # count of shared namespaced keys (S6-8 semantics)


def disjoint_pairs(model: NetworkModel, candidates, *, basis: str, level: str,
                   best_effort: bool, top_n: int,
                   endpoints: "tuple[str, str] | None" = None,
                   ) -> List[PlacementPair]:
    """O(k^2) pairwise scan. Fully-disjoint pairs (shared == empty) first; if none
    and best_effort, min-overlap pairs (ranked by count of shared keys, S6-8 —
    count of namespaced keys, not physical severity). Returns up to top_n pairs.

    `endpoints`, when given as (src_node, dst_node) -- the demand's TRUE
    optical endpoints -- is passed through to placement_footprint_keys so
    endpoint exclusion never depends on a Placement's internal storage order
    (see placement_footprint_keys' docstring)."""
    keyed = [(c, placement_footprint_keys(model, c, basis=basis, level=level,
                                          endpoints=endpoints))
             for c in candidates]
    disjoint: List[PlacementPair] = []
    overlapping: List[PlacementPair] = []
    for i in range(len(keyed)):
        ci, ki = keyed[i]
        for j in range(i + 1, len(keyed)):
            cj, kj = keyed[j]
            shared = ki & kj
            if not shared:
                disjoint.append(PlacementPair(ci, cj, True, (), (), 0))
            else:
                assets, groups = split_shared_keys(shared)
                overlapping.append(
                    PlacementPair(ci, cj, False, assets, groups, len(shared)))
    if disjoint:
        return disjoint[:top_n]
    if best_effort and overlapping:
        overlapping.sort(key=lambda p: p.overlap)
        return overlapping[:top_n]
    return []
