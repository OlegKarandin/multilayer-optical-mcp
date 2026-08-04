from __future__ import annotations
import math
from collections import defaultdict
from typing import Any, Dict, List
from .network import NetworkModel
from . import ip_routing as _ipr


def _safe_float(x):
    """JSON-safety boundary for QoT-derived floats. `-inf` is used internally
    as the failed-asset margin sentinel (whatif.inject_failure) so the
    margin-feasibility gate reads it as infeasible; `+inf`/NaN also occur
    (no-noise GSNR, missing-baseline margin_before). Python's json module
    happily emits the bare `Infinity`/`-Infinity`/`NaN` tokens, which are not
    valid JSON per RFC 8259 and a strict client parser rejects. Replace a
    non-finite float with a same-named JSON string sentinel here, at the
    serialization boundary, so callers doing real math on QoTState/DegradationRow
    fields upstream keep working with actual +-inf/NaN; only the outgoing dict
    is sanitized. Finite floats and non-float values pass through unchanged."""
    if isinstance(x, float) and not math.isfinite(x):
        if x != x:  # NaN is the only float that is not equal to itself
            return "NaN"
        return "Infinity" if x > 0 else "-Infinity"
    return x


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
            qot = {"gsnr_db": _safe_float(qs.gsnr_db), "osnr_db": _safe_float(qs.osnr_db),
                   "margin_db": _safe_float(qs.margin_db),
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
            lp_id = model.get_ip_link_lightpath_id(ip_id)
            if lp_id is not None:
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
        "exhaustive": res.exhaustive,
    }


def feasibility_result_dict(res) -> Dict[str, Any]:
    return {
        "feasible": res.feasible,
        "clashes": [{"oms_id": c.oms_id, "slot": c.slot} for c in res.clashes],
    }


def _spectrum_assignment(a) -> dict:
    return {
        "oms_path": _oms_path(a.oms_path),
        "slot_index": a.slot_index,
        "center_freq_hz": a.center_freq_hz,
        "mode_id": a.mode_id,
        "gsnr_db": a.gsnr_db,
    }


def _demand_placement(p) -> dict:
    return {
        "demand_id": p.demand_id,
        "working": _spectrum_assignment(p.working),
        "protection": _spectrum_assignment(p.protection) if p.protection else None,
    }


def placement_result_dict(res) -> Dict[str, Any]:
    """Serializer for solve_rsa results. solve_allocation now uses
    allocation_result_dict, not this."""
    return {
        "status": res.status.value,
        "placements": [_demand_placement(p) for p in res.placements],
        "unplaced": [{"demand_id": did, "reason": r} for did, r in res.unplaced],
    }


def _new_lp_run(r) -> dict:
    """Serialize a NewLightpathRun (shared shape with restoration_result_dict)."""
    return {"oms_sequence": list(r.oms_sequence), "lam": r.lam,
            "mode_id": r.mode_id, "gsnr_db": r.gsnr_db,
            "bitrate_gbps": r.bitrate_gbps}


def _allocation_placement(p) -> dict:
    return {
        "demand_id": p.demand_id,
        "lever": p.lever,
        "reused_lightpaths": list(p.reused_lightpaths),
        "new_lightpaths": [_new_lp_run(r) for r in p.new_lightpaths],
        "protection_reused": list(p.protection_reused),
        "protection_new": [_new_lp_run(r) for r in p.protection_new],
        "restored_gbps": p.restored_gbps,
        "shortfall_gbps": p.shortfall_gbps,
    }


def allocation_result_dict(res) -> Dict[str, Any]:
    """Serializer for solve_allocation's Placement-based AllocationResult (distinct
    from placement_result_dict, which serves solve_rsa's slot-based shape)."""
    return {
        "status": res.status.value,
        "placements": [_allocation_placement(p) for p in res.placements],
        "unplaced": [{"demand_id": did, "reason": r} for did, r in res.unplaced],
    }


def ip_topology_dict(model: NetworkModel) -> Dict[str, Any]:
    load = _ipr.offered_load_per_link(model)
    links = []
    for link in model.list_ip_links():
        try:
            cap = model.ip_link_capacity_gbps(link.id)
        except LookupError:
            cap = None
        links.append({
            "id": link.id,
            "a_router": link.a_router,
            "z_router": link.z_router,
            "lightpath_id": link.lightpath_id,
            "capacity_gbps": cap,
            "load_gbps": load[link.id],
        })
    return {
        "routers": [_router(r) for r in model._routers.values()],
        "ip_links": links,
    }


def grooming_map_dict(model: NetworkModel) -> Dict[str, Any]:
    gm = _ipr.build_grooming_map(model)
    return {
        "by_service": {sid: list(lps) for sid, lps in gm.by_service.items()},
        "by_lightpath": {lp: list(svcs) for lp, svcs in gm.by_lightpath.items()},
    }


def ip_routing_result_dict(res) -> Dict[str, Any]:
    return {
        "utilizations": [
            {
                "ip_link_id": u.ip_link_id,
                "offered_gbps": u.offered_gbps,
                "capacity_gbps": u.capacity_gbps,
                "utilization": u.utilization,
                "down": u.down,
            }
            for u in res.utilizations
        ],
        "congestion": list(res.congested_links),
        "restored": list(res.restored_services),
        "dropped": {
            "services": [
                {"service_id": d.service_id, "reason": d.reason,
                 "on_link": d.on_link}
                for d in res.dropped_services
            ],
            "down_links": list(res.down_links),
            "overflow_gbps": res.overflow_gbps,
        },
    }


def affected_services_dict(model: NetworkModel, asset_id: str) -> Dict[str, Any]:
    return {"asset_id": asset_id,
            "services": list(_ipr.affected_services(model, asset_id))}


def margin_sweep_dict(rows) -> dict:
    return {"fragile": [
        {"lightpath_id": r.lightpath_id, "margin_db": _safe_float(r.margin_db),
         "gsnr_db": _safe_float(r.gsnr_db), "mode_feasible": r.mode_feasible} for r in rows]}


def max_feasible_mode_dict(rows) -> List[dict]:
    return [
        {"lightpath_id": r.lightpath_id, "current_mode": r.current_mode,
         "max_feasible_mode": r.max_feasible_mode, "direction": r.direction}
        for r in rows]


def degradation_report_dict(report) -> dict:
    return {
        "asset_id": report.asset_id,
        "nf_delta": report.nf_delta,
        "loss_delta": report.loss_delta,
        "crossings": list(report.crossings),
        "rows": [
            {"lightpath_id": r.lightpath_id, "margin_before": _safe_float(r.margin_before),
             "margin_after": _safe_float(r.margin_after), "feasible_before": r.feasible_before,
             "feasible_after": r.feasible_after, "crossed": r.crossed,
             "within_threshold": r.within_threshold} for r in report.rows],
    }


def failure_report_dict(report) -> dict:
    return {"failed_assets": list(report.failed_assets),
            "downed_lightpaths": list(report.downed_lightpaths)}


def sensitivity_result_dict(res) -> Dict[str, Any]:
    return {
        "delta_margin_db": res.delta_margin_db,
        "delta_gsnr_db": res.delta_gsnr_db,
        "rows": [
            {"element_id": r.element_id,
             "gsnr_contribution_delta_db": r.gsnr_contribution_delta_db,
             "ase_contribution_delta_db": r.ase_contribution_delta_db,
             "nli_contribution_delta_db": r.nli_contribution_delta_db}
            for r in res.rows
        ],
    }


def restoration_result_dict(res) -> Dict[str, Any]:
    """Serialize a RestorationResult to a JSON-safe dict."""
    def _new_lp(r) -> dict:
        return {"oms_sequence": list(r.oms_sequence), "lam": r.lam,
                "mode_id": r.mode_id, "gsnr_db": r.gsnr_db,
                "bitrate_gbps": r.bitrate_gbps}

    def _cand(c) -> dict:
        return {"lever": c.lever,
                "reused_lightpaths": list(c.reused_lightpaths),
                "new_lightpaths": [_new_lp(r) for r in c.new_lightpaths],
                "restored_gbps": c.restored_gbps,
                "shortfall_gbps": c.shortfall_gbps,
                "cost_vector": {k: _safe_float(v) for k, v in c.cost_vector.items()}}

    return {"status": res.status.value,
            "service_id": res.service_id,
            "demand_gbps": res.demand_gbps,
            "candidates": [_cand(c) for c in res.candidates]}


def objective_result_dict(res) -> Dict[str, Any]:
    """Serialize an ObjectiveResult: the 7-term cost vector plus the weighted
    scalar (total_margin is the sole benefit term, already subtracted in scalar).
    `total_margin`/`scalar` can carry a non-finite float (e.g. a failed asset's
    -inf margin sentinel propagating into the sum) -- sanitized at this boundary
    like every other QoT-derived float (see _safe_float)."""
    return {"spectrum_used": res.spectrum_used, "transponders": res.transponders,
            "max_util": res.max_util, "dropped_traffic": res.dropped_traffic,
            "added_latency": res.added_latency, "total_margin": _safe_float(res.total_margin),
            "services_at_risk": res.services_at_risk, "scalar": _safe_float(res.scalar)}


def route_service_result_dict(res) -> Dict[str, Any]:
    """Serialize a RouteServiceResult: unprotected candidate menu (`candidates`)
    or protected disjoint-pair menu (`pairs`), whichever the request populated."""
    def _cand(c) -> dict:
        return {"lever": c.lever,
                "reused_lightpaths": list(c.reused_lightpaths),
                "new_lightpaths": [_new_lp_run(r) for r in c.new_lightpaths],
                "restored_gbps": c.restored_gbps,
                "shortfall_gbps": c.shortfall_gbps,
                "cost_vector": {k: _safe_float(v) for k, v in c.cost_vector.items()}}

    def _pair(p) -> dict:
        return {"working": _cand(p.working), "protection": _cand(p.protection),
                "disjoint": p.disjoint,
                "shared_assets": list(p.shared_assets),
                "shared_groups": list(p.shared_groups),
                "cost_vector": {k: _safe_float(v) for k, v in p.cost_vector.items()}}

    return {"status": res.status.value,
            "service_id": res.service_id,
            "demand_gbps": res.demand_gbps,
            "protected": res.protected,
            "candidates": [_cand(c) for c in res.candidates],
            "pairs": [_pair(p) for p in res.pairs]}


def validation_report_dict(report) -> Dict[str, Any]:
    """Serialize a ValidationReport: ok/num_states plus the typed violation list,
    each violation carrying its state_index, transient flag, and remediation
    detail (Decision 4: the detail points the agent at the right fix).
    `detail` is `Dict[str, Any]` -- not every value is a float (e.g.
    SPECTRUM_CLASH's "lightpaths" is a list of ids) -- so each value is
    sanitized individually via _safe_float, which passes non-float values
    through unchanged; this catches the float-valued keys (margin_db, gsnr_db,
    deficit_db, offload_gbps, overflow_gbps, ...) that can carry a non-finite
    QoT-derived float (e.g. a failed asset's -inf margin sentinel)."""
    return {
        "ok": report.ok,
        "num_states": report.num_states,
        "violations": [
            {"type": v.type.value, "state_index": v.state_index,
             "asset_id": v.asset_id, "transient": v.transient,
             "detail": {k: _safe_float(dv) for k, dv in v.detail.items()}}
            for v in report.violations
        ],
    }


def _jsonify_diff(diff):
    """Normalize a registry diff to JSON-native lists (the per-registry deltas
    carry tuples from snapshots._delta; JSON has no tuple). A rejection diff maps
    a registry-shaped key to a plain string ({"error": msg}) — passed through."""
    if diff is None:
        return None
    return {reg: {k: list(v) for k, v in delta.items()}
            if isinstance(delta, dict) else delta
            for reg, delta in diff.items()}


def commit_result_dict(result) -> Dict[str, Any]:
    """Serialize a CommitResult (status/applied/failed + the simulated diff for a
    dry-run and the embedded validation report)."""
    return {
        "status": result.status,
        "dry_run": result.dry_run,
        "applied_ops": result.applied_ops,
        "failed_ops": result.failed_ops,
        "intended_snapshot_id": result.intended_snapshot_id,
        "validation": validation_report_dict(result.validation)
        if result.validation is not None else None,
        "diff": _jsonify_diff(result.diff),
        "failures": [
            {"op_index": f.op_index, "op": f.op_repr, "error": f.error}
            for f in result.failures
        ],
    }


def drift_report_dict(report) -> Dict[str, Any]:
    """Serialize a DriftReport (in_sync + typed drift entries from reconcile)."""
    return {
        "in_sync": report.in_sync,
        "drift": [
            {"registry": d.registry, "kind": d.kind, "asset_id": d.asset_id}
            for d in report.drift
        ],
    }
