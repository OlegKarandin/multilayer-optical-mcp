# src/multilayer_optical_mcp/model/optical_topology_import.py
"""Optical-only half of Phase 6a: OpticalNetworkModel <- an abstract node/edge
graph. Zero IP-layer imports, module scope or otherwise — this is the file a
downstream, IP-free consumer of the optical model depends on. The IP-layer
build (routers on top) lives in topology_import.py, which imports
populate_optical from here; the dependency runs one way, same direction as
NetworkModel(OpticalNetworkModel).

populate_optical(n, graph, loss_coef) builds the optical layer (fiber types,
per node roadm_/trx_<id>, per edge _edge_spans -> split_link_into_spans when
span_lengths_km is absent/inconsistent, then _add_directed_oms TWICE for both
directions, each emitting a booster + per-span (fiber, amp) + an OMS whose
elements start at roadm_<src>) onto an already-constructed model instance.
optical_model_from_abstract_graph builds a bare OpticalNetworkModel with it.
The synthesis half (synthesize.py, adapter.py) is Stage 3/4 of the inspection
roadmap.

Importer assumptions (recorded explicitly, from the inspection roadmap):
- One ROADM/Transceiver per node; roadm_<id> / trx_<id> naming.
- Every edge is bidirectional -> two independent directed OMS with independent
  amp chains (amp_<src>_<dst>_* vs amp_<dst>_<src>_*). The importer builds
  correct reverse-chain impairments, which the adapter (post-S4-2/A2 fix) now
  actually propagates into BACKWARD QoT.
"""
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


from .assets import Amplifier, Fiber, FiberType, OMS, ROADM, Transceiver
from .modes import ModeRegistry
from .optical_network import OpticalNetworkModel

DEFAULT_AMP_NF_DB = 5.5
DEFAULT_AMP_GAIN_DB = 20.0
SSMF_LOSS_COEF_DB_PER_KM = 0.2


def _edge_spans(edge: Dict[str, Any]) -> List[float]:
    """Resolve an edge's per-span lengths from span_lengths_km, or derive them
    from length_km via split_link_into_spans. If `edge["num_spans"]` is present,
    it must agree with the resolved span count (S3-add-5 follow-up) — a graph
    whose num_spans disagrees with the actual span count now fails loudly
    instead of importing silently."""
    spans = edge.get("span_lengths_km")
    if spans:
        # Addendum-2: span_lengths_km are authoritative when present. If they do
        # NOT sum to length_km, that is a data error — fail loudly rather than
        # silently discard them and re-derive a different span count (which would
        # then mis-index amplifier_nf_db by span position).
        if abs(sum(spans) - float(edge["length_km"])) >= 1.0:
            raise ValueError(
                f"edge {edge.get('src')}-{edge.get('dst')}: span_lengths_km "
                f"{spans} sum to {sum(spans):g} km, not length_km "
                f"{edge['length_km']:g} km"
            )
        resolved = [float(s) for s in spans]
    else:
        resolved = split_link_into_spans(float(edge["length_km"]))

    num_spans = edge.get("num_spans")
    if num_spans is not None and int(num_spans) != len(resolved):
        raise ValueError(
            f"edge {edge.get('src')}-{edge.get('dst')}: num_spans={num_spans} "
            f"disagrees with the resolved span count {len(resolved)}"
        )
    return resolved


def populate_optical(
    n: OpticalNetworkModel,
    graph: Dict[str, Any],
    fiber_loss_coef_db_per_km: float,
) -> None:
    """Build the optical layer (fiber types, ROADMs, transceivers, OMS) onto an
    already-constructed model instance. Takes the base type — every call here is
    an inherited ``OpticalNetworkModel`` method, so this never touches a router
    or any other IP-layer concept, and works unchanged whether *n* is a bare
    ``OpticalNetworkModel`` or the ``NetworkModel`` subclass."""
    # Addendum-1: register a FiberType for every distinct fiber_type named in the
    # graph (SSMF always). Without this, add_fiber raises ValueError on the first
    # non-SSMF edge. The graph carries only a type *name*, not per-type physics, so
    # non-SSMF varieties take FiberType's default dispersion/effective_area/pmd and
    # the shared loss coefficient — best-effort until the graph schema carries them.
    fiber_type_names = {"SSMF"} | {
        str(e.get("fiber_type", "SSMF")) for e in graph["edges"]
    }
    for name in sorted(fiber_type_names):
        n.register_fiber_type(
            FiberType(type_variety=name, loss_coef_db_per_km=fiber_loss_coef_db_per_km)
        )

    for node in graph["nodes"]:
        nid = str(node["id"])
        n.add_roadm(ROADM(id=f"roadm_{nid}"))
        n.add_transceiver(Transceiver(id=f"trx_{nid}", site=nid))

    for edge in graph["edges"]:
        spans = _edge_spans(edge)
        # Addendum-2: an explicit amplifier_nf_db must line up with the span count
        # one-for-one. A shorter/longer list otherwise silently truncates or
        # DEFAULT-fills per-span NFs (nfs[i] fallback in _add_directed_oms).
        nfs = edge.get("amplifier_nf_db")
        if nfs is None:
            nfs = [DEFAULT_AMP_NF_DB] * len(spans)
        elif len(nfs) != len(spans):
            raise ValueError(
                f"edge {edge.get('src')}-{edge.get('dst')}: amplifier_nf_db has "
                f"{len(nfs)} entries but the link resolves to {len(spans)} spans"
            )
        fiber_type = edge.get("fiber_type", "SSMF")
        for src, dst in ((edge["src"], edge["dst"]), (edge["dst"], edge["src"])):
            _add_directed_oms(n, str(src), str(dst), spans, nfs,
                              fiber_type=fiber_type,
                              fiber_loss_coef=fiber_loss_coef_db_per_km)


def optical_model_from_abstract_graph(
    graph: Dict[str, Any],
    *,
    modes: ModeRegistry,
    fiber_loss_coef_db_per_km: float = SSMF_LOSS_COEF_DB_PER_KM,
) -> OpticalNetworkModel:
    """Build a standalone, IP-free OpticalNetworkModel from an abstract node/edge
    graph. Importing this module pulls in zero IP-layer modules — see the
    isolation test in tests/model/test_optical_topology_import.py."""
    n = OpticalNetworkModel(modes=modes)
    populate_optical(n, graph, fiber_loss_coef_db_per_km)
    return n


def _add_directed_oms(
    n: OpticalNetworkModel, src: str, dst: str,
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
        # Fiber.a_end/z_end now match the fiber's real predecessor/successor in
        # the elements chain (S3-add-4 follow-up): span 0's predecessor is the
        # booster (elements = [roadm_src, booster, fiber_0, amp_0, ...]), not the
        # ROADM directly — the ROADM has the booster between it and fiber_0.
        n.add_fiber(Fiber(id=fid, a_end=booster_id if i == 0 else f"amp_{src}_{dst}_{i-1}",
                          z_end=aid, length_km=float(span_km), type_variety=fiber_type))
        nf_i = float(nfs[i]) if i < len(nfs) else DEFAULT_AMP_NF_DB
        gain = round(span_km * fiber_loss_coef, 2)
        n.add_amplifier(Amplifier(id=aid, type_variety="advanced_toy",
                                  gain_db=gain, nf_db=nf_i))
        elements.extend([fid, aid])
    n.add_oms(OMS(id=f"oms_{src}_{dst}", src_node_id=src, dst_node_id=dst,
                  elements=tuple(elements)))
