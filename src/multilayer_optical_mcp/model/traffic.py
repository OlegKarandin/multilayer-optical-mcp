"""Seeded gravity demand generator — the pure statistical core of the
operating-network builder (see docs/superpowers/specs/2026-07-14-...).

`generate_demands` is a deterministic function of (model, seed, params): no solver,
no QoT. It emits the frozen `solve_allocation` demand schema
`[{id, src, dst, demand_gbps, protected}]` (src/dst = optical node ids), which
`model/scenario.py` feeds through the packer to manufacture a loaded network.

Gravity model: demand between two nodes ∝ mass(u)·mass(v) / dist(u,v)^alpha, with
mass = node degree in the optical (OMS) graph (a hub proxy; override via
`node_mass`) and dist = shortest fiber length. `seed` enters ONLY through a
deterministic per-node mass jitter, so seed 0/1/2 yield distinct-but-reproducible
scenarios while a fixed seed is byte-stable.

This module is disaster-agnostic infrastructure: it knows nothing about events,
geography, or weather (CLAUDE.md hard rule).
"""
from __future__ import annotations

from typing import Dict, List, Optional

import networkx as nx
import numpy as np

from .network import NetworkModel
from .solvers import oms_length_km


def generate_demands(
    model: NetworkModel,
    *,
    seed: int,
    scale: float,
    alpha: float = 1.0,
    unit_gbps: float = 100.0,
    protected_fraction: float = 0.3,
    node_mass: Optional[Dict[str, float]] = None,
    mass_jitter: float = 0.15,
) -> List[dict]:
    """Emit a gravity-weighted demand list totalling ~`scale` Gbps of offered load.

    Args:
        seed: drives deterministic mass jitter (reproducible variety).
        scale: total offered volume (Gbps) spread across pairs by gravity weight.
        alpha: distance exponent in the gravity kernel.
        unit_gbps: quantum; each pair's offered volume becomes `round(offered/unit)`
            demands of `unit_gbps` each (nearest — pairs activate gradually as scale
            grows, keeping the scenario driver's search monotone).
        protected_fraction: share of demands (highest-gravity first) flagged
            `protected` — protection lands on the busiest, hub-incident corridors.
        node_mass: optional per-node mass override; default is OMS-graph degree.
        mass_jitter: fractional half-width of the per-node multiplicative jitter.

    Deterministic given (model, seed, all params). Disconnected pairs are skipped.
    """
    nodes = sorted({r.site for r in model.list_routers()})

    # Undirected optical graph: one edge per node pair, shortest length if parallel.
    g: nx.Graph = nx.Graph()
    g.add_nodes_from(nodes)
    for oms in model.list_oms():
        u, v = oms.src_node_id, oms.dst_node_id
        km = oms_length_km(model, oms.id)
        if g.has_edge(u, v):
            g[u][v]["km"] = min(g[u][v]["km"], km)
        else:
            g.add_edge(u, v, km=km)

    deg = dict(g.degree())
    rng = np.random.default_rng(seed)
    mass: Dict[str, float] = {}
    for nid in nodes:                    # sorted → stable RNG draw sequence
        base = float(node_mass[nid]) if node_mass is not None else float(deg.get(nid, 0))
        jitter = 1.0 + (rng.random() * 2.0 - 1.0) * mass_jitter
        mass[nid] = base * jitter

    dist = dict(nx.all_pairs_dijkstra_path_length(g, weight="km"))

    # Gravity weight per ordered pair (u != v) with a path and positive distance.
    pair_w: List[tuple] = []             # (u, v, w)
    total_w = 0.0
    for u in nodes:
        for v in nodes:
            if u == v:
                continue
            d = dist.get(u, {}).get(v)
            if d is None or d <= 0.0:
                continue
            w = mass[u] * mass[v] / (d ** alpha)
            if w <= 0.0:
                continue
            pair_w.append((u, v, w))
            total_w += w

    # Expand each pair's offered volume into unit-sized demand records (emission
    # order = sorted pairs, then unit index) carrying their gravity weight.
    records: List[dict] = []             # {src, dst, w}
    if total_w > 0.0:
        for u, v, w in pair_w:
            offered = scale * w / total_w
            n_units = int(round(offered / unit_gbps))
            for _ in range(n_units):
                records.append({"src": u, "dst": v, "w": w})

    # Protection: the top `protected_fraction` of records by gravity weight
    # (deterministic tie-break on src, dst, emission index).
    k = int(round(protected_fraction * len(records)))
    ranked = sorted(range(len(records)),
                    key=lambda i: (-records[i]["w"], records[i]["src"],
                                   records[i]["dst"], i))
    protected_idx = set(ranked[:k])

    demands: List[dict] = []
    for i, rec in enumerate(records):
        demands.append({
            "id": f"d{i:04d}",
            "src": rec["src"],
            "dst": rec["dst"],
            "demand_gbps": unit_gbps,
            "protected": i in protected_idx,
        })
    return demands
