"""Replay a plan op-by-op on a clone and return a typed violation list, checked
at EVERY intermediate state. No physics here: the validator recomputes QoT via
the adapter and reads simulate_ip_routing / ip_link_capacity_gbps, which already
encode the margin-feasibility gate. Margin is never set (it is an output).

Decision 4 (plan doc): the violation *type* is for triage; the *detail* carries
enough locally-cheap context (retune candidates, feasible downshift modes,
overflow, shared assets) for the agent to choose a feasible reoptimization
without re-running a solver just to learn what kind of failure it faces.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from ..gnpy_adapter.adapter import recompute_qot_under_loading
from .ip_routing import (
    simulate_ip_routing, offered_load_per_link, reserved_capacity_per_link,
)
from .network import NetworkModel
from .plan import Plan, PlanError, apply_op, service_oms_sequence
from .qot_results import QoTResultStore
from .solvers import check_disjointness
from .spectrum import SpectrumGrid, free_slots_along
from .whatif import loading_from_model


class ViolationType(str, Enum):
    MODE_INFEASIBLE = "mode_infeasible"          # lightpath margin < 0 (QoT gate)
    SPECTRUM_CLASH = "spectrum_clash"            # two lightpaths on one (oms, slot)
    IP_LINK_OVERLOAD = "ip_link_overload"        # utilization > 1
    DROPPED_TRAFFIC = "dropped_traffic"          # service lost (incl. unrouted), above tol
    DISJOINTNESS_COLLAPSE = "disjointness_collapse"
    PROTECTION_NOT_VIABLE = "protection_not_viable"          # disjoint but unusable failover
    PROTECTION_OVERSUBSCRIBED = "protection_oversubscribed"  # reserved 1:1 capacity double-booked
    INVALID_PLAN = "invalid_plan"                # malformed / bad-reference / dup-id op


@dataclass(frozen=True)
class Violation:
    """A single typed finding from validate_plan. `detail`'s keys are
    type-specific remediation context (Decision 4, module docstring) -- e.g.
    IP_LINK_OVERLOAD's "offload_gbps" (offered_gbps - capacity_gbps) and
    PROTECTION_OVERSUBSCRIBED's "overflow_gbps" (working + reserved -
    capacity) are computed differently and are NOT interchangeable across
    violation types; read the finding-builder for the type in question
    (_ip_findings, _protection_oversubscription_findings, etc.) to see its
    exact keys."""
    type: ViolationType
    state_index: int                 # which intermediate state (0 = after first op)
    asset_id: Optional[str]
    # True iff this finding is absent at the final state AND the final state
    # actually evaluated this finding type (see _CHECKED_ALL/_CHECKED_CLASH_ONLY
    # and the `transient` computation in validate_plan) -- a finding whose type
    # went unevaluated at the final state (e.g. short-circuited by a spectrum
    # clash) is left non-transient rather than assumed resolved.
    transient: bool
    detail: dict


@dataclass(frozen=True)
class ValidationReport:
    violations: Tuple[Violation, ...]
    num_states: int

    @property
    def ok(self) -> bool:
        return not self.violations


# A finding before transient-tagging: (type, asset_id, detail).
_Finding = Tuple[ViolationType, Optional[str], dict]

_RETUNE_CANDIDATE_LIMIT = 8          # cap free-slot lists so detail stays compact


def _mode_infeasible_findings(model: NetworkModel) -> List[_Finding]:
    out: List[_Finding] = []
    for lp in model.list_lightpaths():
        try:
            st = model.get_qot_state(lp.id)
        except LookupError:
            continue
        if st.margin_db < 0:
            cur = model.modes.get(lp.mode_id)
            # Lower-rate modes the current GSNR WOULD satisfy. Non-empty => a
            # downshift recovers the link (capacity falls, but it stays up);
            # empty => GSNR is below every mode's threshold, so reroute/repair,
            # not a downshift, is the only fix.
            downshift = [m.id for m in sorted(model.modes.list(),
                                              key=lambda m: -m.bitrate_gbps)
                         if m.required_gsnr_db <= st.gsnr_db
                         and m.bitrate_gbps < cur.bitrate_gbps]
            out.append((ViolationType.MODE_INFEASIBLE, lp.id, {
                "margin_db": st.margin_db,
                "gsnr_db": st.gsnr_db,
                "required_gsnr_db": cur.required_gsnr_db,
                "deficit_db": cur.required_gsnr_db - st.gsnr_db,
                "feasible_downshift_modes": downshift,
            }))
    return out


def _spectrum_clash_findings(model: NetworkModel) -> List[_Finding]:
    grid = SpectrumGrid.default()
    # Build the per-OMS occupancy from on-grid carriers only. An off-grid carrier
    # is not a clash we model here, and a read-path check must never raise, so we
    # skip it rather than calling build_spectrum_state (which raises on off-grid).
    state: Dict[str, int] = {}
    occ: Dict[Tuple[str, int], List[str]] = {}
    for lp in model.list_lightpaths():
        try:
            slot = grid.slot_of(lp.center_freq_hz)
        except ValueError:
            continue
        for oms_id in lp.oms_sequence:
            state[oms_id] = state.get(oms_id, 0) | (1 << slot)
            occ.setdefault((oms_id, slot), []).append(lp.id)
    out: List[_Finding] = []
    for (oms_id, slot), lps in occ.items():
        if len(lps) > 1:
            # Per clashing lightpath, the slots free on EVERY OMS of its path. A
            # lightpath's own (clash) slot is occupied in `state`, so it is
            # correctly absent from its own candidates. Empty list => path
            # exhausted -> reroute; non-empty => retune to any listed slot.
            retune: Dict[str, List[int]] = {}
            for lp_id in sorted(lps):
                lp = model.get_lightpath(lp_id)
                free_mask = free_slots_along(state, lp.oms_sequence, grid)
                slots = [i for i in range(grid.num_slots) if free_mask & (1 << i)]
                retune[lp_id] = slots[:_RETUNE_CANDIDATE_LIMIT]
            out.append((ViolationType.SPECTRUM_CLASH, oms_id, {
                "slot": slot,
                "lightpaths": sorted(lps),
                "retune_candidates": retune,
            }))
    return out


def _ip_findings(model: NetworkModel, dropped_tolerance_gbps: float) -> List[_Finding]:
    res = simulate_ip_routing(model)
    out: List[_Finding] = []
    util_by_link = {u.ip_link_id: u for u in res.utilizations}
    for link_id in res.congested_links:
        u = util_by_link[link_id]
        out.append((ViolationType.IP_LINK_OVERLOAD, link_id, {
            "utilization": u.utilization,
            "offered_gbps": u.offered_gbps,
            "capacity_gbps": u.capacity_gbps,
            # Distinct key from PROTECTION_OVERSUBSCRIBED's "overflow_gbps" -- the
            # two use different formulas (offered-capacity here vs.
            # working+reserved-capacity there); see the Violation docstring.
            "offload_gbps": u.offered_gbps - u.capacity_gbps,   # how much to offload
        }))
    # simulate_ip_routing is failover-aware: it already classifies every lost
    # service (link_down / link_removed / unrouted) and excludes those restored
    # onto protection. Just gate the aggregate lost demand on the tolerance.
    dropped = res.dropped_services
    total = sum(model.get_service(d.service_id).demand_gbps for d in dropped)
    if total > dropped_tolerance_gbps:
        for d in dropped:
            out.append((ViolationType.DROPPED_TRAFFIC, d.service_id,
                        {"reason": d.reason, "on_link": d.on_link,
                         "demand_gbps": model.get_service(d.service_id).demand_gbps}))
    return out


def _disjointness_findings(model: NetworkModel, basis: str, level: str) -> List[_Finding]:
    out: List[_Finding] = []
    for svc in model.list_services():
        if not svc.working_path or not svc.protection_path:
            continue
        a = service_oms_sequence(model, svc.working_path)
        b = service_oms_sequence(model, svc.protection_path)
        # Thread the service's TRUE optical endpoints through explicitly rather
        # than letting check_disjointness infer each path's endpoints
        # positionally from its own oms_sequence[0]/[-1] -- same mechanism/fix
        # as multilayer_disjoint.disjoint_pairs' `endpoints` kwarg. Working and
        # protection share the same demand, hence the same true endpoints; the
        # router->optical-node resolution mirrors route_service.py's src/dst.
        endpoints = (model.get_router(svc.src_router).site,
                     model.get_router(svc.dst_router).site)
        res = check_disjointness(model, a, b, basis, level,
                                 endpoints_a=endpoints, endpoints_b=endpoints)
        if not res.disjoint:
            out.append((ViolationType.DISJOINTNESS_COLLAPSE, svc.id,
                        {"basis": basis, "level": level,
                         "shared_assets": list(res.shared_assets),
                         "shared_groups": list(res.shared_groups)}))
    return out


def _protection_viability_findings(model: NetworkModel) -> List[_Finding]:
    """Endpoint check (never transient): a service's protection path must be
    USABLE on failover, not merely disjoint from working. Disjointness says the
    two won't fail together; viability says protection can actually carry the load
    when it's called. Fail a service if any protection link is dark (capacity 0 —
    margin-gated down, removed, or with no recorded QoT) or the bottleneck
    capacity along the path is below the demand it would inherit."""
    out: List[_Finding] = []
    for svc in model.list_services():
        if not svc.protection_path:
            continue
        dead: List[str] = []
        caps: List[Tuple[str, float]] = []
        for ip_id in svc.protection_path:
            try:
                cap = model.ip_link_capacity_gbps(ip_id)
            except (KeyError, LookupError):
                dead.append(ip_id)               # protection link missing / no QoT
                continue
            if cap <= 0:
                dead.append(ip_id)               # present in topology but optically dark
            caps.append((ip_id, cap))
        if dead:
            prot_cap, bottleneck = 0.0, None
        elif caps:
            bottleneck, prot_cap = min(caps, key=lambda kc: kc[1])
        else:
            prot_cap, bottleneck = 0.0, None
        if dead or prot_cap < svc.demand_gbps:
            out.append((ViolationType.PROTECTION_NOT_VIABLE, svc.id, {
                "demand_gbps": svc.demand_gbps,
                "protection_capacity_gbps": prot_cap,
                "dead_links": dead,              # repair/re-light these
                "bottleneck_link": bottleneck,   # or re-route/upgrade this
            }))
    return out


def _protection_oversubscription_findings(model: NetworkModel) -> List[_Finding]:
    """1:1 admission (endpoint, never transient): a link must hold its working
    traffic PLUS the full protection reservation of every service protected over
    it (dedicated, summed across services). working_load + reserved > capacity
    means the reserved failover capacity is not actually guaranteed — the link is
    oversubscribed. This enforcement is what lets _protection_viability_findings
    keep its cheap nominal check (no contention is possible once this passes)."""
    working = offered_load_per_link(model)          # working-only nominal load
    reserved = reserved_capacity_per_link(model)
    out: List[_Finding] = []
    for link in model.list_ip_links():
        try:
            cap = model.ip_link_capacity_gbps(link.id)
        except (KeyError, LookupError):
            continue                                 # unknown capacity: not assessable here
        committed = working[link.id] + reserved[link.id]
        # Gate on a GENUINE reservation: with reserved == 0, `committed > cap`
        # collapses to `working > cap`, a plain capacity overload that
        # IP_LINK_OVERLOAD already reports (via simulate_ip_routing's congestion
        # check). Without this gate this violation double-reports that overload
        # under the wrong type, implying a protection reservation that doesn't
        # exist (audit finding: PROTECTION_OVERSUBSCRIBED firing with reserved=0).
        if cap > 0 and reserved[link.id] > 0 and committed > cap:
            reservers = sorted(svc.id for svc in model.list_services()
                               if link.id in svc.protection_path)
            out.append((ViolationType.PROTECTION_OVERSUBSCRIBED, link.id, {
                "working_gbps": working[link.id],
                "reserved_gbps": reserved[link.id],
                "capacity_gbps": cap,
                "overflow_gbps": committed - cap,
                "reserving_services": reservers,
            }))
    return out


def _op_target(op) -> Optional[str]:
    """Best-effort asset id an op acts on, for INVALID_PLAN.asset_id."""
    if hasattr(op, "lightpath"):
        return op.lightpath.id
    for attr in ("lightpath_id", "service_id"):
        if hasattr(op, attr):
            return getattr(op, attr)
    return None


def recompute_if_possible(model: NetworkModel, store: QoTResultStore) -> None:
    """Recompute QoT for all lightpaths under the model's own loading. No-op when
    there are no lightpaths (nothing to propagate). Best-effort: an adapter/GNPy
    exception during recompute must not escape to validate_plan's caller as a raw
    exception -- it would otherwise surface as a misleading INVALID_PLAN ("the
    plan is malformed") when the actual failure is a QoT recompute error, not a
    plan-structure problem. Swallow and leave this state's QoT at its prior
    value, same best-effort contract commit.py's own callers of this function
    already follow."""
    if model.list_lightpaths():
        try:
            recompute_qot_under_loading(model=model, store=store,
                                        loading=loading_from_model(model))
        except Exception:
            pass


def validate_plan(
    model: NetworkModel,
    plan: Plan,
    *,
    store: QoTResultStore,
    basis: str = "physical",
    level: str = "link",
    dropped_tolerance_gbps: float = 0.0,
) -> ValidationReport:
    """Replay `plan` on a clone of `model`, recompute QoT after each op, and
    collect typed violations at every intermediate state. A finding present at a
    non-final state but absent at the final state is tagged transient (the
    make-before-break window). Ground truth (`model`) is never mutated.
    """
    work = model.clone()

    # Every finding type state_findings is capable of returning, so the loop
    # below can tell "final state checked this type and found nothing" (safe to
    # call an earlier hit transient) apart from "final state never ran this
    # check" (unknown -- must NOT be tagged transient). See _CHECKED_ALL /
    # _CHECKED_CLASH_ONLY below.
    _CHECKED_ALL = frozenset({
        ViolationType.SPECTRUM_CLASH, ViolationType.MODE_INFEASIBLE,
        ViolationType.IP_LINK_OVERLOAD, ViolationType.DROPPED_TRAFFIC,
    })
    _CHECKED_CLASH_ONLY = frozenset({ViolationType.SPECTRUM_CLASH})

    def state_findings(m: NetworkModel) -> Tuple[List[_Finding], frozenset]:
        # Spectrum first: a clashed state has two carriers on one frequency, so
        # QoT is undefined (you cannot light both). Report the clash + its
        # retune/reroute discriminator and skip the QoT-dependent checks rather
        # than drive GNPy with a degenerate overlapping-carrier loading. This
        # short-circuit is physically correct; it is NOT the bug. The bug (fixed
        # below) is treating a state whose QoT/IP checks were skipped as if it
        # had confirmed those finding types clear -- return which types were
        # actually evaluated so the caller can tell "confirmed clear" apart from
        # "undetermined."
        clashes = _spectrum_clash_findings(m)
        if clashes:
            return clashes, _CHECKED_CLASH_ONLY
        recompute_if_possible(m, store)
        findings = (_mode_infeasible_findings(m)
                    + _ip_findings(m, dropped_tolerance_gbps))
        return findings, _CHECKED_ALL

    def endpoint_findings(m: NetworkModel) -> List[_Finding]:
        return (_disjointness_findings(m, basis, level)
                + _protection_viability_findings(m)
                + _protection_oversubscription_findings(m))

    if not plan.ops:
        # Validate the standing state (e.g. a fresh disjointness / protection audit).
        state, _checked = state_findings(work)
        findings = state + endpoint_findings(work)
        violations = [Violation(t, 0, a, False, d) for (t, a, d) in findings]
        return ValidationReport(violations=tuple(violations), num_states=1)

    # (state_index -> (findings, finding-types actually evaluated)). State 0 is
    # after the first op.
    per_state: List[Tuple[List[_Finding], frozenset]] = []
    for op in plan.ops:
        try:
            apply_op(work, op)
        except PlanError as exc:
            # A structurally invalid plan (bad reference, duplicate id, unknown
            # op) cannot be validated past the failed op. Surface it as a single
            # typed violation instead of raising — tool results are typed lists,
            # never exceptions (CLAUDE.md). state_index/op_index = ops applied so
            # far = the 0-based index of the op that failed.
            bad = Violation(ViolationType.INVALID_PLAN, len(per_state),
                            _op_target(op), False,
                            {"op_index": len(per_state), "message": str(exc)})
            return ValidationReport(violations=(bad,), num_states=len(per_state))
        per_state.append(state_findings(work))

    final_index = len(per_state) - 1
    final_findings, final_checked_types = per_state[final_index]
    final_keys = {(t, a) for (t, a, _) in final_findings}

    violations: List[Violation] = []
    for idx, (findings, _checked) in enumerate(per_state):
        for (t, a, d) in findings:
            # Only call an earlier finding transient when the final state
            # actually EVALUATED that finding type and came back clean for
            # (t, a). If the final state short-circuited that check (e.g. its
            # own spectrum clash skipped QoT/IP evaluation), whether the earlier
            # finding still holds there is undetermined -- default to NOT
            # transient rather than falsely reporting it resolved.
            transient = (idx != final_index
                         and t in final_checked_types
                         and (t, a) not in final_keys)
            violations.append(Violation(t, idx, a, transient, d))

    # Endpoint properties of the committed plan (never transient): disjointness
    # collapse and protection-path viability / oversubscription.
    for (t, a, d) in endpoint_findings(work):
        violations.append(Violation(t, final_index, a, False, d))

    return ValidationReport(violations=tuple(violations), num_states=len(per_state))
