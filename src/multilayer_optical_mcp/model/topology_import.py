# src/multilayer_optical_mcp/model/topology_import.py
"""IP-layer half of Phase 6a: NetworkModel <- an abstract node/edge graph.

model_from_abstract_graph builds the optical layer via
optical_topology_import._populate_optical (fiber types, per node roadm_/trx_/
router_<id>, per edge two directed OMS) onto a NetworkModel, then adds one
router_<id> per node. The pure-optical half (no IP-layer imports, ever) lives
in optical_topology_import.py; this file is the IP-coupled side, so it imports
Router/NetworkModel at module scope like any other IP-layer module. The
synthesis half (synthesize.py, adapter.py) is Stage 3/4 of the inspection
roadmap.

Importer assumptions (recorded explicitly, from the inspection roadmap):
- One ROADM/Transceiver/Router per node; roadm_<id> / trx_<id> / router_<id>
  naming. `Router.site == optical-node id` is the src_router -> optical-node
  convention Stage 7 restoration depends on.
- Every edge is bidirectional -> two independent directed OMS with independent
  amp chains (amp_<src>_<dst>_* vs amp_<dst>_<src>_*). The importer builds
  correct reverse-chain impairments, which the adapter (post-S4-2/A2 fix) now
  actually propagates into BACKWARD QoT.
"""
from __future__ import annotations

from typing import Any, Dict

from .ip_assets import Router
from .modes import ModeRegistry
from .network import NetworkModel
from .optical_topology_import import SSMF_LOSS_COEF_DB_PER_KM, _populate_optical


def model_from_abstract_graph(
    graph: Dict[str, Any],
    *,
    modes: ModeRegistry,
    fiber_loss_coef_db_per_km: float = SSMF_LOSS_COEF_DB_PER_KM,
) -> NetworkModel:
    """Build a NetworkModel (optical layer plus one router per node) from an
    abstract node/edge graph."""
    n = NetworkModel(modes=modes)
    _populate_optical(n, graph, fiber_loss_coef_db_per_km)
    for node in graph["nodes"]:
        n.add_router(Router(id=f"router_{node['id']}", site=str(node["id"])))
    return n
