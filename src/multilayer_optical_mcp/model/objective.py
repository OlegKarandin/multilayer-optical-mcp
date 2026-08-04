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
    """The lightpath ids under a service's working IP path (its declared
    intent). A dangling ip-link id (removed lightpath/link, a documented
    valid state) contributes nothing rather than raising."""
    out = []
    for ip_id in svc.working_path:
        lp_id = model.get_ip_link_lightpath_id(ip_id)
        if lp_id is not None:
            out.append(lp_id)
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


# Real-committer id namespaces (allocation.py's _pack): "cand" (default,
# non-protected + protected-working) and "prot" (explicit, protected-
# protection). Any scoring/throwaway caller of apply_candidate/
# provision_new_runs MUST use a prefix outside this set -- score_pair uses
# "score-work"/"score-prot", score_candidate uses "score-cand" below.
# _mint_unique is defense in depth: it also handles a REAL committer
# colliding with its OWN prior commits (allocation.py's _pack re-processing a
# demand id after a failure cut the demand's earlier lightpath).
RESERVED_COMMITTER_PREFIXES = frozenset({"cand", "prot"})


def _assert_prefix_not_reserved(prefix: str) -> None:
    """A caller's id-minting prefix must either BE a real committer's exact
    reserved value ("cand" default / "prot" explicit) or must not COLLIDE
    with the reserved namespace at all -- even a near-miss like "cand-explore"
    risks _mint_unique's f"{template}-{n}" suffixing scheme picking an id a
    real committer mints later. Scoring/throwaway callers (score_candidate,
    score_pair) use a "score-" prefix precisely to stay clear of this; this
    assertion is what actually enforces that, instead of leaving it as a
    comment for the next caller to remember on their own."""
    if prefix in RESERVED_COMMITTER_PREFIXES:
        return
    colliding = [p for p in RESERVED_COMMITTER_PREFIXES if prefix.startswith(p)]
    if colliding:
        raise ValueError(
            f"id-minting prefix {prefix!r} collides with reserved committer "
            f"namespace {sorted(colliding)}; use a prefix that does not start "
            f"with a reserved value (e.g. \"score-\" + something)")


def _mint_unique(work, registry_attr: str, template: str) -> str:
    """Return `template` if free in `work`'s `registry_attr` dict (e.g.
    '_lightpaths' or '_ip_links'), else the first `template-N` (N=2,3,...)
    that is free. Prevents two independent id-minting producers -- a scorer
    and a real committer, or a real committer colliding with its own prior
    commits on a re-run -- from silently overwriting or crashing on the same
    asset id."""
    registry = getattr(work, registry_attr)
    if template not in registry:
        return template
    n = 2
    while f"{template}-{n}" in registry:
        n += 1
    return f"{template}-{n}"


def _snapshot_lightpath_qot(work) -> Dict[str, QoTState]:
    """Snapshot every currently-recorded lightpath QoT state on `work`. Called
    right after `work = model.clone()`, before any candidate provisioning, so
    the returned dict captures every PRE-EXISTING ("bystander") lightpath's
    QoT as it stood before this scoring pass touched anything. Only ids that
    actually have a recorded state are included -- LookupError is swallowed
    rather than inventing a value for a lightpath that never had one (a
    freshly-provisioned, not-yet-recomputed lightpath legitimately has none;
    see ip_routing.py's "unknown" link-status state)."""
    out = {}
    for lp in work.list_lightpaths():
        try:
            out[lp.id] = work.get_qot_state(lp.id)
        except LookupError:
            pass
    return out


def _restore_bystander_qot(work, snapshot: Dict[str, QoTState]) -> None:
    """Re-apply every snapshotted lightpath's QoT onto `work`, undoing any
    wipe that this scoring pass's OWN provisioning inflicted on a
    PRE-EXISTING lightpath via cross-lightpath OMS-sharing invalidation
    (NetworkModel._invalidate_qot_sharing_oms) -- the "bystander" case: a
    lightpath that is not itself part of the candidate being scored (this
    includes both reused_lightpaths the candidate grooms onto and any other
    lightpath that merely happens to share an OMS with one of the candidate's
    new runs).

    Unconditional and idempotent: every snapshotted id is re-applied
    regardless of whether that particular lightpath was actually invalidated
    (a lightpath the candidate never touched gets its unchanged value written
    back -- a no-op), matching the flat accumulate-then-reseed pattern
    allocation.py's _pack already uses for cross-demand reseeding. No
    per-lightpath bookkeeping of "was this one actually wiped" is needed.

    Why restoring the OLD value is CORRECT for feasibility/capacity, and a
    deliberate, bounded approximation (not exact) for total_margin -- read
    both halves, they are NOT the same claim:

    Every mode in this codebase is accepted under FillPolicy.FULL (a
    worst-case comb -- every non-probe grid slot treated as occupied
    regardless of real provisioning). FillPolicy's own docstring states the
    governing invariant in full: "By GSNR monotonicity in interferer count, a
    FULL-accepted mode remains feasible under any lighter real load, so the
    operating recompute stays ACTUAL and is not gated by this policy"
    (spectrum.py) -- note the second clause: the OPERATING model's recorded
    QoT is NOT generally the FULL acceptance value. scenario.py's settle pass
    explicitly overwrites every lightpath's seeded-at-acceptance QoT with a
    recompute under the real, ACTUAL committed comb (adapter.py's
    recompute_qot_under_loading), so a bystander in a built operating network
    typically carries an ACTUAL-loading margin, not a FULL one.

    What this means in practice:
    - FEASIBILITY (hence derived capacity, hence max_util/dropped_traffic/
      congestion evidence) IS exact regardless of which policy produced the
      snapshotted value: FULL acceptance guarantees margin >= 0 under ANY
      lighter real load, so a bystander that was up stays up and one that was
      down stays down -- restoring the old margin's SIGN, and therefore its
      derived capacity, can never disagree with a real recompute. This is the
      property the "erasing evidence of congestion" fix actually relies on.
    - TOTAL_MARGIN is an optimistic BOUND, not an exact reproduction, when the
      snapshotted value came from an ACTUAL-comb recompute: a genuinely new
      co-OMS channel does lower the bystander's true GSNR by a small amount
      (one added interferer in a fixed-width comb). The restored value is
      strictly better than the pre-fix behavior (which effectively scored a
      wiped bystander's margin as 0, an unbounded error), and the error it
      does carry is applied identically to every candidate that touches this
      bystander, so relative ranking between such candidates is undistorted;
      it is not zero error against ground truth.

    FillPolicy.ACTUAL is a caller-settable parameter (see allocation.py,
    scenario.py) that is never SELECTED by any in-repo call site today; if a
    caller starts routinely requesting ACTUAL-scored candidates, the
    total_margin approximation above still applies (it does not depend on
    which policy scored the CANDIDATE, only on which policy produced the
    BYSTANDER's snapshotted value) -- no change needed here for that case.
    """
    for lp_id, state in snapshot.items():
        work.set_qot_state(lp_id, state)


# A (lp_id, QoTState) pair seeded during provisioning of one or more new runs.
# apply_candidate/provision_new_runs return these so a caller that provisions
# many runs across many calls (allocation.py's _pack, across both legs and
# across demands) can do one final corrective re-seed pass after everything is
# provisioned -- see _pack's post-loop re-seed and the module-level note above
# RESERVED_COMMITTER_PREFIXES for why cross-run invalidation is possible here.
SeededQoT = Tuple[Tuple[str, QoTState], ...]


def _provision_and_seed_run(work, run, lp_id, ipl_id, site_to_router, grid):
    """Provision one NewLightpathRun as a lightpath+IP link via the real
    apply_op path, then SEED its QoT from the run's gsnr_db (real provision does
    not seed QoT; real commit reaches the same numbers via a post-commit
    recompute). Shared by apply_candidate (working leg) and score_pair
    (protection leg) so both go through identical provisioning logic. Returns
    (a_router, z_router, lp_id, ipl_id, state) -- the ACTUAL ids used, which may
    differ from the requested lp_id/ipl_id if either collided with an id
    already present in `work` (see _mint_unique), plus the QoTState just seeded
    (a caller provisioning multiple runs may need to re-apply this seed after a
    LATER run's provisioning invalidates it via cross-lightpath OMS-sharing
    invalidation -- see NetworkModel._invalidate_qot_sharing_oms)."""
    lp_id = _mint_unique(work, "_lightpaths", lp_id)
    ipl_id = _mint_unique(work, "_ip_links", ipl_id)
    a = site_to_router[run.src_node]
    z = site_to_router[run.dst_node]
    apply_op(work, ProvisionLightpath(
        lightpath=Lightpath(id=lp_id, oms_sequence=run.oms_sequence,
                            mode_id=run.mode_id, center_freq_hz=grid.freq(run.lam)),
        ip_link=IPLink(id=ipl_id, a_router=a, z_router=z, lightpath_id=lp_id)))
    req = work.modes.get(run.mode_id).required_gsnr_db
    state = QoTState(gsnr_db=run.gsnr_db, osnr_db=run.gsnr_db,
                     margin_db=run.gsnr_db - req)
    work.set_qot_state(lp_id, state)
    return a, z, lp_id, ipl_id, state


def apply_candidate(work, placement, service, *, prefix="cand") -> SeededQoT:
    """Materialize a Placement on `work` (a clone): provision each new run as a
    lightpath+IP link, seed QoT from the run's gsnr_db, then reroute the
    service's working path onto the placement.

    Returns the `(lp_id, QoTState)` pairs seeded for this placement's new runs.
    A run provisioned earlier in this SAME call can have its just-seeded QoT
    wiped by a LATER run's provisioning if the two share an OMS (Task 4's
    cross-lightpath invalidation firing on Task 6's now-common co-located-
    siblings scenario). This function re-applies every one of ITS OWN seeds in
    one corrective pass after all of its new runs are provisioned (below),
    so by the time it returns, every lightpath it just provisioned carries
    correct QoT -- regardless of how much invalidation churn happened between
    the individual provisioning calls. That makes the function internally
    consistent on its own; a caller that provisions MULTIPLE such calls in a
    loop (allocation.py's _pack, across demands and legs) can still have a
    LATER call's provisioning wipe an EARLIER call's (already-correct) seed,
    which is why _pack additionally re-applies every returned seed of its own
    after each iteration (and once more after the whole loop, as a safety
    net) -- see _pack's per-iteration re-seed comment."""
    _assert_prefix_not_reserved(prefix)
    grid = SpectrumGrid.default()
    site_to_router = {r.site: r.id for r in work.list_routers()}
    lp_to_iplink = {l.lightpath_id: l for l in work.list_ip_links()}
    segments = []
    seeded = []
    # reused legs: reuse their existing IP link binding
    for lp_id in placement.reused_lightpaths:
        link = lp_to_iplink[lp_id]
        segments.append((link.a_router, link.z_router, link.id))
    # new legs: provision lightpath + IP link, seed QoT
    for i, run in enumerate(placement.new_lightpaths):
        lp_id = f"lp-{prefix}-{service.id}-{i}"
        ipl_id = f"ipl-{prefix}-{service.id}-{i}"
        a, z, lp_id, ipl_id, state = _provision_and_seed_run(
            work, run, lp_id, ipl_id, site_to_router, grid)
        segments.append((a, z, ipl_id))
        seeded.append((lp_id, state))
    ip_path = _stitch_ip_path(segments, service.src_router, service.dst_router)
    apply_op(work, RerouteService(service_id=service.id, ip_path=ip_path))
    # Corrective re-seed: a later run in the loop above may have wiped an
    # earlier run's just-seeded QoT via cross-lightpath OMS-sharing
    # invalidation. Re-applying every seed THIS call collected is the last
    # write for each of them, making this call internally consistent before
    # it returns -- see the docstring above.
    for lp_id, state in seeded:
        work.set_qot_state(lp_id, state)
    return tuple(seeded)


def provision_new_runs(work, placement, service, *, prefix) -> Tuple[Tuple[str, ...], SeededQoT]:
    """Provision each new run of `placement` as a lightpath + IP link and seed its
    QoT, WITHOUT rerouting any service. Used for a protection leg: a 1:1 idle
    reserve whose transponder/spectrum/margin cost must count but which carries no
    IP load. Shared by score_pair (which discards both return values -- protection
    must not be routed while scoring, see score_pair's docstring) and
    solve_allocation's protected commit (which uses the ip_path to stitch
    Service.protection_path, and the seed pairs to re-seed after the whole run
    completes -- see allocation.py's _pack). Returns `(ip_path, seeded)`:
    `ip_path` is the segments would stitch into, same construction as
    apply_candidate, including reused legs (previously silently dropped -- a
    protection leg that grooms onto a survivor lightpath had no IP-link segment
    tracked at all); `seeded` is the `(lp_id, QoTState)` pairs seeded for this
    placement's new runs (see apply_candidate's docstring for why a caller
    provisioning multiple runs must keep and re-apply these)."""
    _assert_prefix_not_reserved(prefix)
    grid = SpectrumGrid.default()
    site_to_router = {r.site: r.id for r in work.list_routers()}
    lp_to_iplink = {l.lightpath_id: l for l in work.list_ip_links()}
    segments = []
    seeded = []
    for lp_id in placement.reused_lightpaths:
        link = lp_to_iplink[lp_id]
        segments.append((link.a_router, link.z_router, link.id))
    for i, run in enumerate(placement.new_lightpaths):
        lp_id = f"lp-{prefix}-{service.id}-{i}"
        ipl_id = f"ipl-{prefix}-{service.id}-{i}"
        a, z, lp_id, ipl_id, state = _provision_and_seed_run(
            work, run, lp_id, ipl_id, site_to_router, grid)
        segments.append((a, z, ipl_id))
        seeded.append((lp_id, state))
    ip_path = _stitch_ip_path(segments, service.src_router, service.dst_router)
    # Corrective re-seed: same rationale as apply_candidate's -- a later run
    # provisioned above may have wiped an earlier run's just-seeded QoT via
    # cross-lightpath OMS-sharing invalidation. Re-apply every seed THIS call
    # collected so this call is internally consistent before it returns.
    for lp_id, state in seeded:
        work.set_qot_state(lp_id, state)
    return ip_path, tuple(seeded)


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
    """Materialize `placement` on a throwaway clone and score it.

    Bystander protection: provisioning the candidate's new run(s) can wipe the
    recorded QoT of a PRE-EXISTING lightpath that merely happens to share an
    OMS with one of them (NetworkModel._invalidate_qot_sharing_oms fires on
    any lightpath sharing an OMS with a newly-added one, not just this
    candidate's own runs). evaluate_objective's total_margin loop silently
    skips any lightpath with no recorded QoT, and ip_link_capacity_gbps raises
    LookupError for one too (read by simulate_ip_routing as "unknown", not
    congested/down) -- so an un-restored bystander's margin contribution
    vanishes from the score, AND, if that bystander was actually congested or
    overloaded, the evidence of that congestion (its utilization/overflow)
    goes invisible right along with it. That can rank a candidate that
    "erases the evidence" of a real problem ABOVE one that never touches the
    bystander -- the opposite of correct scoring. We snapshot every
    pre-existing lightpath's QoT before provisioning and restore it after, so
    the score always reflects every lightpath's true state. See
    _restore_bystander_qot's docstring for why restoring the OLD value (not
    leaving it wiped, not recomputing) is exact for feasibility/capacity and
    a small, one-sided, non-distorting approximation for total_margin --
    NOT an exact reproduction in every case."""
    work = model.clone()
    bystanders = _snapshot_lightpath_qot(work)
    apply_candidate(work, placement, service, prefix="score-cand")
    _restore_bystander_qot(work, bystanders)
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
    already-protected service, an in-scope restoration use case.

    Bystander protection: same rationale as score_candidate's docstring --
    provisioning either leg's new run(s) can wipe the recorded QoT of a
    PRE-EXISTING lightpath outside this pair (not working, not protection)
    that happens to share an OMS with one of them, silently dropping its
    margin from total_margin and hiding any real congestion it carried from
    max_util/dropped_traffic. We snapshot every pre-existing lightpath's QoT
    before either leg is provisioned and restore it after both legs' own
    seed-reapplication below -- see _restore_bystander_qot's docstring for
    exactly what this restore does and does not guarantee (exact for
    feasibility/capacity; a small, non-distorting approximation for
    total_margin)."""
    work = model.clone()
    bystanders = _snapshot_lightpath_qot(work)
    seeded_working = apply_candidate(work, working, service, prefix="score-work")
    # provision protection's new lightpaths (no reroute) so their cost is counted
    _ip_path, seeded_protection = provision_new_runs(work, protection, service, prefix="score-prot")
    # Each call already re-seeds its OWN runs before returning (see apply_candidate/
    # provision_new_runs), but when working and protection share an OMS -- legal
    # under a relaxed basis (srlg/risk_group/union), or under best_effort=True's
    # minimum-overlap fallback -- protection's provisioning can still invalidate
    # a working-leg lightpath's QoT that this call already collected. Re-apply
    # both legs' seeds together, after both are provisioned, so total_margin/
    # max_util reflect every lightpath this candidate pair actually costs.
    for lp_id, state in (*seeded_working, *seeded_protection):
        work.set_qot_state(lp_id, state)
    # Restore any PRE-EXISTING (neither working nor protection) lightpath's QoT
    # that either leg's provisioning wiped via cross-lightpath OMS-sharing
    # invalidation. Order after the candidate-own-reseed above is not load-
    # bearing (disjoint id sets -- a bystander is by definition not one of
    # this pair's own seeded runs) but keeps the existing mechanism intact and
    # simply adds bystander coverage alongside it.
    _restore_bystander_qot(work, bystanders)
    return evaluate_objective(work, weights)
