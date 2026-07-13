# src/multilayer_optical_mcp/model/objective.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .network import NetworkModel
from .spectrum import SpectrumGrid, build_spectrum_state
from .ip_routing import simulate_ip_routing
from .whatif import margin_threshold_sweep

_PROP_MS_PER_KM = 0.005   # ~5 us/km one-way fiber propagation


@dataclass(frozen=True)
class ObjectiveResult:
    spectrum_used: int
    transponders: float
    max_util: float
    dropped_traffic: float
    added_latency: float
    total_margin: float
    services_at_risk: int
    scalar: float


def _oms_seq_length_km(model: NetworkModel, oms_sequence) -> float:
    total = 0.0
    for oms_id in oms_sequence:
        for el in model.get_oms(oms_id).elements:
            try:
                total += model.get_fiber(el).length_km
            except (KeyError, LookupError):
                continue          # non-fiber element (amp/roadm)
    return total


def _active_working_lightpaths(model, svc):
    """The lightpath ids under a service's working IP path (its declared intent)."""
    out = []
    for ip_id in svc.working_path:
        out.append(model.get_ip_link(ip_id).lightpath_id)
    return out


def evaluate_objective(model: NetworkModel, weights: Optional[Dict[str, float]] = None,
                       *, spare_transponders: Optional[int] = None,
                       at_risk_threshold_db: float = 1.0) -> ObjectiveResult:
    w = weights or {}
    grid = SpectrumGrid.default()

    spectrum_used = sum(bin(mask).count("1")
                        for mask in build_spectrum_state(model, grid).values())

    tp = 2.0 * len(model.list_lightpaths())
    if spare_transponders is not None:
        tp = max(0.0, tp - float(spare_transponders))

    ipr = simulate_ip_routing(model)
    max_util = max((u.utilization for u in ipr.utilizations
                    if u.utilization is not None), default=0.0)

    dropped_ids = {d.service_id for d in ipr.dropped_services}
    dropped_demand = sum(model.get_service(sid).demand_gbps for sid in dropped_ids)
    # Dropped services carry no link load (active_load skips path==None), so
    # dropped_demand and overflow_gbps cover disjoint traffic -> no double count.
    dropped_traffic = dropped_demand + ipr.overflow_gbps

    added_latency = 0.0
    for svc in model.list_services():
        if svc.id in dropped_ids:
            continue
        for lp_id in _active_working_lightpaths(model, svc):
            added_latency += _PROP_MS_PER_KM * _oms_seq_length_km(
                model, model.get_lightpath(lp_id).oms_sequence)

    total_margin = 0.0
    for lp in model.list_lightpaths():
        try:
            total_margin += model.get_qot_state(lp.id).margin_db
        except LookupError:
            continue

    at_risk_lps = {row.lightpath_id
                   for row in margin_threshold_sweep(model, at_risk_threshold_db)}
    # Skip services already dropped: they are down, not "at risk", and a dropped
    # service's working_path may reference a removed IP link (a valid model state
    # per remove_ip_link) whose lightpath lookup would otherwise KeyError here.
    services_at_risk = sum(
        1 for svc in model.list_services()
        if svc.id not in dropped_ids
        and set(_active_working_lightpaths(model, svc)) & at_risk_lps)

    scalar = (w.get("spectrum_used", 1.0) * spectrum_used
              + w.get("transponders", 1.0) * tp
              + w.get("max_util", 1.0) * max_util
              + w.get("dropped_traffic", 1.0) * dropped_traffic
              + w.get("added_latency", 1.0) * added_latency
              - w.get("total_margin", 1.0) * total_margin
              + w.get("services_at_risk", 1.0) * services_at_risk)

    return ObjectiveResult(spectrum_used, tp, max_util, dropped_traffic,
                           added_latency, total_margin, services_at_risk, scalar)
