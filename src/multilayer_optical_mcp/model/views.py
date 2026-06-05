from __future__ import annotations
from collections import defaultdict
from typing import Any, Dict, List
from .network import NetworkModel


def _fiber(f) -> dict:
    return {"id": f.id, "a_end": f.a_end, "z_end": f.z_end,
            "length_km": f.length_km, "type_variety": f.type_variety}


def _amp(a) -> dict:
    return {"id": a.id, "type_variety": a.type_variety,
            "gain_db": a.gain_db, "nf_db": a.nf_db, "tilt_db": a.tilt_db}


def _oms(o) -> dict:
    return {"id": o.id, "src_node_id": o.src_node_id,
            "dst_node_id": o.dst_node_id, "elements": list(o.elements)}


def _router(r) -> dict:
    return {"id": r.id, "site": r.site}


def _ip_link(link) -> dict:
    return {"id": link.id, "a_router": link.a_router,
            "z_router": link.z_router, "lightpath_id": link.lightpath_id}


def topology_dict(model: NetworkModel, *, layer: str) -> Dict[str, Any]:
    """Layered topology view. `layer` ∈ {'optical', 'ip', 'both'}."""
    if layer not in {"optical", "ip", "both"}:
        raise ValueError(f"layer must be 'optical', 'ip', or 'both', got {layer!r}")
    out: Dict[str, Any] = {}
    if layer in {"optical", "both"}:
        out["fiber_types"] = [{"type_variety": ft.type_variety,
                               "loss_coef_db_per_km": ft.loss_coef_db_per_km}
                              for ft in model.list_fiber_types()]
        out["fibers"] = [_fiber(f) for f in model._fibers.values()]
        out["amplifiers"] = [_amp(a) for a in model._amplifiers.values()]
        out["oms"] = [_oms(o) for o in model.list_oms()]
    if layer in {"ip", "both"}:
        out["routers"] = [_router(r) for r in model._routers.values()]
        out["ip_links"] = [_ip_link(lnk) for lnk in model.list_ip_links()]
    return out


def lightpaths_dict(model: NetworkModel) -> List[dict]:
    out: List[dict] = []
    for lp in model.list_lightpaths():
        try:
            qs = model.get_qot_state(lp.id)
            qot = {"gsnr_db": qs.gsnr_db, "osnr_db": qs.osnr_db,
                   "margin_db": qs.margin_db,
                   "mode_feasible": qs.mode_feasible,
                   "limiting_element_id": qs.limiting_element_id}
        except LookupError:
            qot = None
        out.append({
            "id": lp.id,
            "oms_sequence": list(lp.oms_sequence),
            "mode_id": lp.mode_id,
            "center_freq_hz": lp.center_freq_hz,
            "qot": qot,
        })
    return out


def services_dict(model: NetworkModel) -> Dict[str, Any]:
    services = []
    grooming: Dict[str, List[str]] = defaultdict(list)
    for svc in model.list_services():
        services.append({
            "id": svc.id,
            "src_router": svc.src_router, "dst_router": svc.dst_router,
            "demand_gbps": svc.demand_gbps,
            "working_path": list(svc.working_path),
            "protection_path": list(svc.protection_path),
        })
        for ip_id in svc.working_path:
            lp_id = model.get_ip_link(ip_id).lightpath_id
            grooming[lp_id].append(svc.id)
    return {"services": services, "grooming_map": dict(grooming)}


def traffic_matrix_dict(model: NetworkModel) -> Dict[str, Dict[str, float]]:
    tm: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for svc in model.list_services():
        tm[svc.src_router][svc.dst_router] += svc.demand_gbps
    return {src: dict(dsts) for src, dsts in tm.items()}


def srlgs_dict(model: NetworkModel) -> List[dict]:
    return [{"id": g.id, "asset_ids": list(g.asset_ids)}
            for g in model.list_srlgs()]


def risk_groups_dict(model: NetworkModel) -> List[dict]:
    return [{"id": g.id, "asset_ids": list(g.asset_ids),
             "metadata": dict(g.metadata)}
            for g in model.list_risk_groups()]


def _oms_path(p) -> dict:
    return {"node_sequence": list(p.node_sequence),
            "oms_sequence": list(p.oms_sequence)}


def routing_result_dict(res) -> Dict[str, Any]:
    return {
        "status": res.status.value,
        "paths": [_oms_path(p) for p in res.paths],
    }


def disjointness_result_dict(res) -> Dict[str, Any]:
    return {
        "status": res.status.value,
        "disjoint": res.disjoint,
        "basis": res.basis,
        "level": res.level,
        "path_a": _oms_path(res.path_a) if res.path_a is not None else None,
        "path_b": _oms_path(res.path_b) if res.path_b is not None else None,
        "shared_assets": list(res.shared_assets),
        "shared_groups": list(res.shared_groups),
    }
