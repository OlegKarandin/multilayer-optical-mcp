"""Flood disaster scenario, end-to-end against the real GNPy adapter.

Per CLAUDE-disaster.md, flood is "same machinery [as storm], inverted
filter": a set of assets (buried handholes / low-lying spans, here
hand-constructed since german_17 carries no geo metadata) that working and
protection paths were never checked against at design time. `route_service`
already prefers PHYSICALLY-disjoint working/protection pairs (basis="physical"
default, see route_service.py / allocation.py's `_demand_constraints`), so a
real german_17 build gives protected services whose legs are physically
disjoint but not vetted against any *risk group* -- exactly the gap a
flood-zone risk group exposes.

Opt-in real-adapter test: gated behind MOMCP_RUN_GNPY_E2E=1 via the shared
`german17_built` fixture (tests/e2e/conftest.py). Direct-Python-API
convention throughout, except the explicit MCP-tool-layer check at the end
(tests/test_server.py:35-47's `_call` pattern).
"""
from __future__ import annotations

import asyncio

from multilayer_optical_mcp.model import objective
from multilayer_optical_mcp.model.allocation import make_adapter_evaluator
from multilayer_optical_mcp.model.assets import Service
from multilayer_optical_mcp.model.commit import commit_plan, reconcile
from multilayer_optical_mcp.model.exposure import (
    compute_exposure, oms_seq_asset_set, service_asset_set,
)
from multilayer_optical_mcp.model.ip_routing import simulate_ip_routing
from multilayer_optical_mcp.model.plan import (
    Plan, ProvisionLightpath, RerouteService, apply_op, service_oms_sequence,
)
from multilayer_optical_mcp.model.qot_results import QoTCache, QoTResultStore
from multilayer_optical_mcp.model.route_service import route_service
from multilayer_optical_mcp.model.snapshots import SnapshotStore
from multilayer_optical_mcp.model.solvers import SolverStatus, check_disjointness
from multilayer_optical_mcp.model.validate import ViolationType, validate_plan
from multilayer_optical_mcp.model.whatif import inject_failure
from multilayer_optical_mcp.server import build_app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _physical_span_ids(assets) -> list[str]:
    """fiber_/amp_ ids only, sorted -- excludes oms_/roadm_/lightpath/ip-link
    ids so a picked flood-zone member is a bare physical span, never a shared
    endpoint (CLAUDE.md's endpoint-exclusion point stays a separate concern)."""
    return sorted(a for a in assets if a.startswith("fiber_") or a.startswith("amp_"))


def _pick_correlated_service(model) -> tuple[Service, frozenset[str]]:
    """First protected service (non-empty protection_path) with at least one
    fiber_/amp_ physical asset on each leg, plus a synthetic 2-asset flood
    zone: the lowest sorted physical id from working and from protection.
    Stand-in for "a downstream geo-mapper decided these two physically
    separate spans both sit inside the flood polygon". Deterministic given
    seed=0's build (CLAUDE.md: never hardcode ids)."""
    for svc in model.list_services():
        if not svc.protection_path:
            continue
        working = _physical_span_ids(service_asset_set(model, svc.id, which="working"))
        protection = _physical_span_ids(service_asset_set(model, svc.id, which="protection"))
        if not working or not protection:
            continue
        w_asset, p_asset = working[0], protection[0]
        if w_asset == p_asset:
            continue  # would collapse to a 1-asset zone; keep looking
        return svc, frozenset((w_asset, p_asset))
    raise AssertionError(
        "no protected service with a fiber_/amp_ physical asset on both legs "
        "found in the built german_17 network"
    )


def _tool(app, name, **kwargs):
    """Invoke a real @app.tool() closure directly, sync or coroutine-aware --
    mirrors tests/test_server.py:40-47's `_call` helper."""
    tool = app._tool_manager._tools[name]
    result = tool.fn(**kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _link_physical_span_ids(model, link_id: str) -> tuple[str, ...]:
    """fiber_/amp_ span ids under the lightpath bound to a single IP link --
    what to feed route_service(avoid={"assets": ...}) to route a replacement
    leg around that one link specifically."""
    link = model.get_ip_link(link_id)
    lp = model.get_lightpath(link.lightpath_id)
    return tuple(_physical_span_ids(oms_seq_asset_set(model, lp.oms_sequence)))


def _replan_protection(work, qot, sid: str, *, prefix: str, protected: bool,
                       avoid: dict, ops: list) -> bool:
    """Replan `sid`'s protection leg (via a protected disjoint-pair search when
    `protected=True`, else an unprotected single-candidate search) around
    `avoid`, materialize it on `work`, append the resulting ProvisionLightpath
    /RerouteService ops to `ops`. Returns True on success (an op was applied),
    False if no feasible candidate/pair was found (caller tries the next
    remedy)."""
    svc_v = work.get_service(sid)
    res = route_service(work, qot, sid, protected=protected, basis="risk_group",
                        level="link", avoid=avoid)
    if protected:
        if res.status not in (SolverStatus.SOLUTION, SolverStatus.PARTIAL) or not res.pairs:
            return False
        placement = res.pairs[0].protection
    else:
        if res.status not in (SolverStatus.SOLUTION, SolverStatus.PARTIAL) or not res.candidates:
            return False
        placement = res.candidates[0]
    before = {lp.id for lp in work.list_lightpaths()}
    new_path = objective.provision_new_runs(work, placement, svc_v, prefix=prefix)
    apply_op(work, RerouteService(sid, new_path, which="protection"))
    for lp_id in sorted({lp.id for lp in work.list_lightpaths()} - before):
        lp_obj = work.get_lightpath(lp_id)
        ipl_obj = next((l for l in work.list_ip_links() if l.lightpath_id == lp_id), None)
        ops.append(ProvisionLightpath(lightpath=lp_obj, ip_link=ipl_obj))
    ops.append(RerouteService(sid, new_path, which="protection"))
    return True


def _heal_endpoint_violations(work, qot, rg_id: str, store, *, max_rounds: int = 10):
    """Iteratively discover and remediate EVERY remaining DISJOINTNESS_COLLAPSE
    (under basis="risk_group") and PROTECTION_OVERSUBSCRIBED violation on
    `work`, one service-protection replan per round: a collapse is fixed by
    replanning that service's protection leg away from the risk group (same
    remedy as svc.id's own fix); an oversubscription is fixed by replanning
    one of its reserving services' protection legs away from the oversub'd
    link's own physical span. Mutates `work` in place. Returns (ops,
    converged): ops is the ordered ProvisionLightpath/RerouteService sequence
    applied, replayable against a fresh SnapshotStore; converged is whether
    validate_plan(work, Plan(ops=())) reached report.ok before max_rounds.

    Why this is needed, not just svc.id's own fix: validate.py's endpoint
    findings (disjointness, protection oversubscription) are computed over
    the WHOLE model, not scoped to a plan's own ops (see validate_plan's
    endpoint_findings call), and commit_plan's live path rejects unless the
    ENTIRE report is ok. A real, heuristically-packed network can carry
    violations unrelated to the scenario under test -- a second service that
    happens to also cross the same flood-zone assets, and pre-existing 1:1
    protection-reservation oversubscription from the initial build's packing
    heuristic (CLAUDE.md: solve_allocation makes no optimality claim). Both
    must be cleared for a live commit to ever reach "committed" against this
    fixture, not just svc.id's targeted collapse."""
    ops: list = []
    for round_no in range(max_rounds):
        report = validate_plan(work, Plan(ops=()), store=store,
                               basis="risk_group", level="link")
        if report.ok:
            return tuple(ops), True

        collapse = [v for v in report.violations
                    if v.type is ViolationType.DISJOINTNESS_COLLAPSE]
        oversub = [v for v in report.violations
                   if v.type is ViolationType.PROTECTION_OVERSUBSCRIBED]
        progressed = False

        for v in collapse:
            sid = v.asset_id
            if not work.get_service(sid).protection_path:
                continue
            if _replan_protection(work, qot, sid, prefix=f"heal{round_no}",
                                  protected=True, avoid={"risk_groups": (rg_id,)},
                                  ops=ops):
                progressed = True
                break

        if not progressed:
            for v in oversub:
                avoid_assets = _link_physical_span_ids(work, v.asset_id)
                for sid in sorted(v.detail["reserving_services"]):
                    if _replan_protection(work, qot, sid, prefix=f"heal{round_no}",
                                          protected=False, avoid={"assets": avoid_assets},
                                          ops=ops):
                        progressed = True
                        break
                if progressed:
                    break

        if not progressed:
            return tuple(ops), False

    report = validate_plan(work, Plan(ops=()), store=store,
                           basis="risk_group", level="link")
    return tuple(ops), report.ok


def _op_to_dict(op) -> dict:
    """Serialize a plan.py op back to the MCP `plan_from_dict` JSON schema, so
    the MCP-layer check commits the EXACT same ops the model layer did."""
    if isinstance(op, ProvisionLightpath):
        return {
            "op": "provision_lightpath",
            "lightpath": {
                "id": op.lightpath.id,
                "oms_sequence": list(op.lightpath.oms_sequence),
                "mode_id": op.lightpath.mode_id,
                "center_freq_hz": op.lightpath.center_freq_hz,
            },
            "ip_link": None if op.ip_link is None else {
                "id": op.ip_link.id,
                "a_router": op.ip_link.a_router,
                "z_router": op.ip_link.z_router,
            },
        }
    if isinstance(op, RerouteService):
        return {"op": "reroute_service", "service_id": op.service_id,
                "ip_path": list(op.ip_path), "which": op.which}
    raise TypeError(f"unexpected op type {op!r}")


# ---------------------------------------------------------------------------
# scenario
# ---------------------------------------------------------------------------

def test_flood_zone_correlation_exposes_gap_and_remedy_lands(german17_built):
    model = german17_built.model
    svc, flood_assets = _pick_correlated_service(model)
    flood_asset_ids = tuple(sorted(flood_assets))

    # ---- Step 1: exposure audit (read-only) --------------------------------
    branch = model.clone()
    rg = branch.define_risk_group("flood-zone-1", flood_asset_ids)
    exposure = compute_exposure(branch, svc.id, rg.id)
    assert exposure.both_intersect is True

    # ---- Step 2: disjointness under two bases -------------------------------
    working_oms = service_oms_sequence(branch, svc.working_path)
    protection_oms = service_oms_sequence(branch, svc.protection_path)

    phys_result = check_disjointness(branch, working_oms, protection_oms,
                                     basis="physical", level="link")
    assert phys_result.disjoint is True   # nothing physical changed

    rg_result = check_disjointness(branch, working_oms, protection_oms,
                                   basis="risk_group", level="link")
    assert rg_result.disjoint is False
    assert rg_result.shared_groups == ("flood-zone-1",)

    # ---- Step 3: validate_plan sees it too, with an empty plan -------------
    store = QoTResultStore()
    report = validate_plan(branch, Plan(ops=()), store=store,
                           basis="risk_group", level="link")
    collapse = [v for v in report.violations
                if v.type is ViolationType.DISJOINTNESS_COLLAPSE and v.asset_id == svc.id]
    assert len(collapse) == 1
    assert collapse[0].detail["basis"] == "risk_group"

    # ---- Step 4a: the flood actually cuts something (correlated failure) ---
    branch2 = branch.clone()
    failure = inject_failure(branch2, flood_asset_ids)
    assert failure.downed_lightpaths

    ipr2 = simulate_ip_routing(branch2)
    dropped_ids = {d.service_id for d in ipr2.dropped_services}
    if svc.id in dropped_ids:
        pass  # protection did not save the service -- exactly the correlation failure mode
    else:
        # grooming absorbed it onto a survivor -- fall back to the
        # bottleneck-capacity signal the brief allows as the alternative.
        work_caps = [branch2.ip_link_capacity_gbps(ip) for ip in svc.working_path]
        prot_caps = [branch2.ip_link_capacity_gbps(ip) for ip in svc.protection_path]
        assert min(work_caps) == 0.0
        assert min(prot_caps) == 0.0

    # ---- Step 4b: contrast -- an independent (uncorrelated) failure does NOT
    #      defeat protection, proving the flood's failure mode is about
    #      correlation, not "any failure beats protection". -------------------
    svc2 = None
    for other in model.list_services():
        if other.id == svc.id or not other.protection_path:
            continue
        w2 = service_oms_sequence(branch, other.working_path)
        p2 = service_oms_sequence(branch, other.protection_path)
        if check_disjointness(branch, w2, p2, basis="risk_group", level="link").disjoint:
            svc2 = other
            break
    assert svc2 is not None, "no protected service unaffected by the flood zone was found"

    svc2_working = _physical_span_ids(service_asset_set(branch, svc2.id, which="working"))
    independent_assets = [a for a in svc2_working if a not in flood_assets]
    assert independent_assets

    branch3 = branch.clone()
    inject_failure(branch3, (independent_assets[0],))
    ipr3 = simulate_ip_routing(branch3)
    assert svc2.id not in {d.service_id for d in ipr3.dropped_services}
    assert svc2.id in ipr3.restored_services   # failed over onto protection, as designed

    # ---- Step 5: replan away from the flood zone -- materialize + commit ---
    cache = QoTCache()
    qot = make_adapter_evaluator(branch, store, cache=cache)
    result = route_service(branch, qot, svc.id, protected=True,
                           basis="risk_group", level="link",
                           avoid={"risk_groups": (rg.id,)})
    assert result.status in (SolverStatus.SOLUTION, SolverStatus.PARTIAL)
    assert result.pairs
    pair = result.pairs[0]
    assert pair.disjoint is True   # under the call's own basis="risk_group"

    # Throwaway clone: materialize svc.id's protection leg, confirming its OWN
    # DISJOINTNESS_COLLAPSE clears. On the REAL german17_built network this is
    # not sufficient for a live commit on its own: validate_plan's endpoint
    # findings (disjointness, protection oversubscription) are computed over
    # the WHOLE model, not scoped to this plan's ops, and commit_plan's live
    # path rejects unless the ENTIRE report is ok. This real, heuristically
    # packed network carries standing violations unrelated to svc.id: another
    # service that happens to also cross the same flood-zone assets, and
    # pre-existing 1:1 protection-reservation oversubscription left over from
    # the initial build's packing heuristic (CLAUDE.md: solve_allocation makes
    # no optimality claim). _heal_endpoint_violations widens the remedy to
    # clear every such violation the same way (replan one affected service's
    # protection leg per round), so the live commit below can genuinely reach
    # "committed" against the real network, not just a hand-picked toy case.
    work = branch.clone()
    before = {lp.id for lp in work.list_lightpaths()}
    new_ip_path = objective.provision_new_runs(work, pair.protection, svc, prefix="flood-fix")
    apply_op(work, RerouteService(svc.id, new_ip_path, which="protection"))
    ops = [ProvisionLightpath(lightpath=work.get_lightpath(lp_id),
                              ip_link=next((l for l in work.list_ip_links()
                                           if l.lightpath_id == lp_id), None))
           for lp_id in sorted({lp.id for lp in work.list_lightpaths()} - before)]
    ops.append(RerouteService(svc.id, new_ip_path, which="protection"))

    fix_report = validate_plan(work, Plan(ops=()), store=store,
                               basis="risk_group", level="link")
    fix_collapse = [v for v in fix_report.violations
                    if v.type is ViolationType.DISJOINTNESS_COLLAPSE and v.asset_id == svc.id]
    assert fix_collapse == []   # svc.id's own fix landed

    heal_ops, converged = _heal_endpoint_violations(work, qot, rg.id, store)
    assert converged, (
        "healing loop did not clear every endpoint violation within the "
        "round budget -- see validate_plan(work, Plan(ops=())) for what's left")
    ops.extend(heal_ops)
    plan = Plan(ops=tuple(ops))
    del work   # throwaway clone discarded -- the real commit path materializes below

    # Drive it through the real commit path: `branch` (pre-remedy, still
    # exposed) seeds a fresh SnapshotStore, so commit_plan itself does the
    # materializing -- not the test.
    snapshots = SnapshotStore(initial=branch)

    dry = commit_plan(snapshots, plan, store_results=store, dry_run=True,
                      basis="risk_group", level="link")
    assert dry.status == "dry_run"
    assert dry.validation.ok is True   # whole network clean once the plan's ops are applied

    live = commit_plan(snapshots, plan, store_results=store, dry_run=False,
                       confirm=True, basis="risk_group", level="link")
    assert live.status == "committed"

    drift = reconcile(snapshots, live.intended_snapshot_id)
    assert drift.in_sync is True
    assert drift.drift == ()

    # ---- MCP-tool-layer check ------------------------------------------------
    # Build the app on a fresh clone of the ORIGINAL (unmutated) model -- the
    # equivalent of "branch" before step 1 defined the risk group, since
    # define_risk_group rejects a duplicate id and the tool call below
    # re-creates it through the tool surface instead of reusing `branch`'s.
    mcp_model = model.clone()
    mcp_snapshots = SnapshotStore(initial=mcp_model)
    app = build_app(model=mcp_model, snapshots=mcp_snapshots, results=QoTResultStore())

    rg_dict = _tool(app, "define_risk_group", rg_id="flood-zone-1",
                    asset_ids=list(flood_asset_ids))
    assert rg_dict["id"] == "flood-zone-1"
    assert tuple(sorted(rg_dict["asset_ids"])) == flood_asset_ids

    exposure_dict = _tool(app, "get_exposure", service_id=svc.id,
                          risk_group_id="flood-zone-1")
    assert exposure_dict["both_intersect"] == exposure.both_intersect
    assert exposure_dict["both_intersect"] is True
    assert exposure_dict["working_intersection"] == list(exposure.working_intersection)
    assert exposure_dict["protection_intersection"] == list(exposure.protection_intersection)

    # commit_plan tool: the exact same combined ops as the model-layer `plan`
    # (svc.id's flood-fix protection leg plus every healing-round reroute),
    # asserted to match the model-layer dry-run/live statuses exactly.
    mcp_plan = {"ops": [_op_to_dict(op) for op in plan.ops]}

    mcp_dry = _tool(app, "commit_plan", plan=mcp_plan, dry_run=True,
                    basis="risk_group", level="link")
    assert mcp_dry["status"] == dry.status

    mcp_live = _tool(app, "commit_plan", plan=mcp_plan, dry_run=False, confirm=True,
                     basis="risk_group", level="link")
    assert mcp_live["status"] == live.status

    # reroute_service tool: called AFTER the commit above (which already
    # provisioned the "flood-fix" IP links via its own embedded
    # RerouteService op) -- calling it beforehand would raise, since
    # set_service_protection_path's contiguity check requires those IP links
    # to already exist. Re-applying the same protection path here is
    # idempotent and confirms the dedicated reroute_service tool is wired to
    # the same underlying mechanism and returns the same path.
    reroute_dict = _tool(app, "reroute_service", service_id=svc.id,
                         ip_path=list(new_ip_path), which="protection")
    assert reroute_dict["protection_path"] == list(new_ip_path)
