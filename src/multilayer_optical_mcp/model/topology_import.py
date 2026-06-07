from __future__ import annotations

import math
from typing import Any, Dict, List


def split_link_into_spans(
    length_km: float,
    target_span_km: float = 80.0,
    min_span_km: float = 20.0,
) -> List[float]:
    """Split a link into balanced spans near *target_span_km*.

    1. n_min = ceil(length/100), n_max = ceil(length/40), clamped to >= 1.
    2. For each n in [n_min, n_max], span_len = length/n; skip if < min_span_km.
    3. Pick n minimising |span_len - target_span_km|.
    4. Return n equal spans, last adjusted so the sum equals length exactly.
    """
    n_min = max(1, math.ceil(length_km / 100.0))
    n_max = max(n_min, math.ceil(length_km / 40.0))

    best_n = None
    best_dev = float("inf")
    for n in range(n_min, n_max + 1):
        span_len = length_km / n
        if span_len < min_span_km:
            continue
        dev = abs(span_len - target_span_km)
        if dev < best_dev:
            best_dev = dev
            best_n = n
    if best_n is None:
        best_n = 1

    base_len = round(length_km / best_n, 2)
    spans = [base_len] * best_n
    spans[-1] = round(length_km - base_len * (best_n - 1), 2)
    return spans


from .assets import (
    Amplifier, Fiber, FiberType, OMS, OpticalNode, ROADM, Router, Transceiver,
)
from .modes import ModeRegistry
from .network import NetworkModel

DEFAULT_AMP_NF_DB = 5.5
DEFAULT_AMP_GAIN_DB = 20.0
SSMF_LOSS_COEF_DB_PER_KM = 0.2


def _edge_spans(edge: Dict[str, Any]) -> List[float]:
    spans = edge.get("span_lengths_km")
    if spans and abs(sum(spans) - edge["length_km"]) < 1.0:
        return [float(s) for s in spans]
    return split_link_into_spans(float(edge["length_km"]))


def model_from_abstract_graph(
    graph: Dict[str, Any],
    *,
    modes: ModeRegistry,
    fiber_loss_coef_db_per_km: float = SSMF_LOSS_COEF_DB_PER_KM,
) -> NetworkModel:
    """Build a NetworkModel optical layer from an abstract node/edge graph."""
    n = NetworkModel(modes=modes)
    n.register_fiber_type(
        FiberType(type_variety="SSMF", loss_coef_db_per_km=fiber_loss_coef_db_per_km)
    )

    for node in graph["nodes"]:
        nid = str(node["id"])
        n.add_optical_node(OpticalNode(id=f"roadm_{nid}", kind="roadm"))
        n.add_roadm(ROADM(id=f"roadm_{nid}"))
        n.add_transceiver(Transceiver(id=f"trx_{nid}", site=nid))
        n.add_router(Router(id=f"router_{nid}", site=nid))

    for edge in graph["edges"]:
        spans = _edge_spans(edge)
        nfs = edge.get("amplifier_nf_db") or [DEFAULT_AMP_NF_DB] * len(spans)
        fiber_type = edge.get("fiber_type", "SSMF")
        for src, dst in ((edge["src"], edge["dst"]), (edge["dst"], edge["src"])):
            _add_directed_oms(n, str(src), str(dst), spans, nfs,
                              fiber_type=fiber_type,
                              fiber_loss_coef=fiber_loss_coef_db_per_km)
    return n


def _add_directed_oms(
    n: NetworkModel, src: str, dst: str,
    spans: List[float], nfs: List[float], *,
    fiber_type: str,
    fiber_loss_coef: float,
) -> None:
    booster_id = f"amp_{src}_{dst}_booster"
    n.add_amplifier(Amplifier(id=booster_id, type_variety="advanced_toy",
                              gain_db=DEFAULT_AMP_GAIN_DB, nf_db=DEFAULT_AMP_NF_DB))
    elements: List[str] = [f"roadm_{src}", booster_id]
    for i, span_km in enumerate(spans):
        fid = f"fiber_{src}_{dst}_{i}"
        aid = f"amp_{src}_{dst}_{i}"
        n.add_fiber(Fiber(id=fid, a_end=f"roadm_{src}" if i == 0 else f"amp_{src}_{dst}_{i-1}",
                          z_end=aid, length_km=float(span_km), type_variety=fiber_type))
        nf_i = float(nfs[i]) if i < len(nfs) else DEFAULT_AMP_NF_DB
        gain = round(span_km * fiber_loss_coef, 2)
        n.add_amplifier(Amplifier(id=aid, type_variety="advanced_toy",
                                  gain_db=gain, nf_db=nf_i))
        elements.extend([fid, aid])
    n.add_oms(OMS(id=f"oms_{src}_{dst}", src_node_id=src, dst_node_id=dst,
                  elements=tuple(elements)))
