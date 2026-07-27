# src/multilayer_optical_mcp/model/objective.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .network import NetworkModel
from .spectrum import SpectrumGrid, build_spectrum_state
from .ip_routing import simulate_ip_routing
from .whatif import margin_threshold_sweep
from .plan import apply_op, ProvisionLightpath, RerouteService
from .assets import Lightpath, IPLink
from .qot import QoTState

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
    """`weights` here is PER-COST-TERM WEIGHTS for the 7-term objective scalar
    (e.g. `{"transponders": 2.0}` maps a cost-vector field name to its multiplier);
    it is not a per-demand priority (that's solve_allocation's `weights`, a
    different meaning of the same parameter name)."""
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


# ---------------------------------------------------------------------------
# Candidate materialization: turn a routing-engine Placement into a scored
# state by provisioning it through the REAL apply_op machinery on a clone, so
# scoring and a real commit can never numerically drift apart.


def _stitch_ip_path(segments, src_router, dst_router):
    """Order (a_router, z_router, ip_id) segments into a contiguous walk
    src_router -> dst_router. Each segment usable in either orientation."""
    remaining = list(segments)
    path = []
    node = src_router
    while node != dst_router and remaining:
        for k, (a, z, ip_id) in enumerate(remaining):
            if a == node:
                path.append(ip_id); node = z; remaining.pop(k); break
            if z == node:
                path.append(ip_id); node = a; remaining.pop(k); break
        else:
            # No segment continues the walk -- should not happen for a real
            # placement. Not re-raised here: the truncated `path` this
            # produces gets handed to apply_op(RerouteService(...)), whose
            # NetworkModel.set_service_working_path (network.py) validates
            # contiguity via is_contiguous_path and raises ValueError, which
            # apply_op re-raises as PlanError (plan.py) -- so a broken walk is
            # always caught, just one call frame downstream of here rather
            # than at the point of truncation.
            break
    return tuple(path)


def _provision_and_seed_run(work, run, lp_id, ipl_id, site_to_router, grid):
    """Provision one NewLightpathRun as a lightpath+IP link via the real
    apply_op path, then SEED its QoT from the run's gsnr_db (real provision does
    not seed QoT; real commit reaches the same numbers via a post-commit
    recompute). Shared by apply_candidate (working leg) and score_pair
    (protection leg) so both go through identical provisioning logic. Returns
    (a_router, z_router) for the caller to build IP-path segments from."""
    a = site_to_router[run.src_node]
    z = site_to_router[run.dst_node]
    apply_op(work, ProvisionLightpath(
        lightpath=Lightpath(id=lp_id, oms_sequence=run.oms_sequence,
                            mode_id=run.mode_id, center_freq_hz=grid.freq(run.lam)),
        ip_link=IPLink(id=ipl_id, a_router=a, z_router=z, lightpath_id=lp_id)))
    req = work.modes.get(run.mode_id).required_gsnr_db
    work.set_qot_state(lp_id, QoTState(gsnr_db=run.gsnr_db, osnr_db=run.gsnr_db,
                                       margin_db=run.gsnr_db - req))
    return a, z


def apply_candidate(work, placement, service, *, prefix="cand") -> None:
    """Materialize a Placement on `work` (a clone): provision each new run as a
    lightpath+IP link, seed QoT from the run's gsnr_db, then reroute the
    service's working path onto the placement."""
    grid = SpectrumGrid.default()
    site_to_router = {r.site: r.id for r in work.list_routers()}
    lp_to_iplink = {l.lightpath_id: l for l in work.list_ip_links()}
    segments = []
    # reused legs: reuse their existing IP link binding
    for lp_id in placement.reused_lightpaths:
        link = lp_to_iplink[lp_id]
        segments.append((link.a_router, link.z_router, link.id))
    # new legs: provision lightpath + IP link, seed QoT
    for i, run in enumerate(placement.new_lightpaths):
        lp_id = f"lp-{prefix}-{service.id}-{i}"
        ipl_id = f"ipl-{prefix}-{service.id}-{i}"
        a, z = _provision_and_seed_run(work, run, lp_id, ipl_id, site_to_router, grid)
        segments.append((a, z, ipl_id))
    ip_path = _stitch_ip_path(segments, service.src_router, service.dst_router)
    apply_op(work, RerouteService(service_id=service.id, ip_path=ip_path))


def provision_new_runs(work, placement, service, *, prefix) -> Tuple[str, ...]:
    """Provision each new run of `placement` as a lightpath + IP link and seed its
    QoT, WITHOUT rerouting any service. Used for a protection leg: a 1:1 idle
    reserve whose transponder/spectrum/margin cost must count but which carries no
    IP load. Shared by score_pair (which discards the return value -- protection
    must not be routed while scoring, see score_pair's docstring) and
    solve_allocation's protected commit (which uses the return value to stitch
    Service.protection_path -- see allocation.py's _pack). Returns the ip_path
    segments would stitch into, same construction as apply_candidate, including
    reused legs (previously silently dropped -- a protection leg that grooms onto a
    survivor lightpath had no IP-link segment tracked at all)."""
    grid = SpectrumGrid.default()
    site_to_router = {r.site: r.id for r in work.list_routers()}
    lp_to_iplink = {l.lightpath_id: l for l in work.list_ip_links()}
    segments = []
    for lp_id in placement.reused_lightpaths:
        link = lp_to_iplink[lp_id]
        segments.append((link.a_router, link.z_router, link.id))
    for i, run in enumerate(placement.new_lightpaths):
        lp_id = f"lp-{prefix}-{service.id}-{i}"
        ipl_id = f"ipl-{prefix}-{service.id}-{i}"
        a, z = _provision_and_seed_run(work, run, lp_id, ipl_id, site_to_router, grid)
        segments.append((a, z, ipl_id))
    return _stitch_ip_path(segments, service.src_router, service.dst_router)


def placement_materializable(model, placement) -> bool:
    """True iff (a) every new run's endpoints resolve to a Router site, and
    (b) every reused lightpath already has a bound IP link. A run ending at a
    router-less optical node cannot be bound to an IP link; a reused lightpath
    with no bound IP link is a valid grooming target for _residual_gbps (a
    lightpath with no IP link bound yields its full mode rate) but has no
    IPLink for apply_candidate to stitch an ip_path segment from. Either case
    is not a feasible service-routing candidate, so the placement is excluded
    here rather than left to crash `apply_candidate` with a bare KeyError."""
    sites = {r.site for r in model.list_routers()}
    if not all(run.src_node in sites and run.dst_node in sites
               for run in placement.new_lightpaths):
        return False
    return all(model.ip_links_for_lightpath(lp_id)
               for lp_id in placement.reused_lightpaths)


def score_candidate(model, placement, service, weights=None) -> ObjectiveResult:
    work = model.clone()
    apply_candidate(work, placement, service)
    return evaluate_objective(work, weights)


def score_pair(model, working, protection, service, weights=None) -> ObjectiveResult:
    """Provision BOTH legs (protection's transponders/spectrum/total_margin count),
    route the working leg. Protection is 1:1 reserved and idle -> not loaded, so it
    contributes no IP load; its cost surfaces via provisioned lightpaths.

    Scratch ids use a "score-" prefix reserved for this throwaway clone, distinct
    from allocation.py's real committer prefixes ("cand" default, "prot"
    explicit) -- score_pair used to reuse "prot", which collided with a
    service's own already-committed protection lightpath (same id scheme) the
    moment route_service was asked to replan the protection leg of an
    already-protected service, an in-scope restoration use case."""
    work = model.clone()
    apply_candidate(work, working, service, prefix="score-work")
    # provision protection's new lightpaths (no reroute) so their cost is counted
    provision_new_runs(work, protection, service, prefix="score-prot")
    return evaluate_objective(work, weights)
