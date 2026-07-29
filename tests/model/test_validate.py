from dataclasses import replace

import pytest

from multilayer_optical_mcp.model.assets import (
    Lightpath, IPLink, Router, Service, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.plan import (
    Plan, ProvisionLightpath, RerouteService, SetModulationFormat, TeardownLightpath,
)
from multilayer_optical_mcp.model.validate import (
    validate_plan, ViolationType,
    _mode_infeasible_findings, _protection_viability_findings,
    _disjointness_findings, _protection_oversubscription_findings,
)
from tests.phase7_topology import new_model, add_bidir_span


def _ip_over_optical(margin_db=2.0, demand=300.0):
    """rA-rB IP link on lpAB (400G, cap 400), carrying `demand`, over a
    synthesizable bidirectional span A<->B."""
    m = new_model()
    add_bidir_span(m, "A", "B", "omsAB")
    m.add_router(Router(id="rA", site="A"))
    m.add_router(Router(id="rB", site="B"))
    m.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",), mode_id="400G",
                              center_freq_hz=193.4e12))
    m.add_ip_link(IPLink(id="ipAB", a_router="rA", z_router="rB", lightpath_id="lpAB"))
    m.add_service(Service(id="svc", src_router="rA", dst_router="rB",
                          demand_gbps=demand, working_path=("ipAB",)))
    m.set_qot_state("lpAB", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=margin_db))
    return m


def _store():
    return QoTResultStore()


def test_empty_plan_is_clean():
    report = validate_plan(_ip_over_optical(), Plan(ops=()), store=_store())
    assert report.ok
    assert report.violations == ()


def test_downshift_below_demand_flags_ip_overload():
    # 300G demand on a 400G link is fine; downshifting to 200G (cap 200) overloads.
    m = _ip_over_optical(demand=300.0)
    plan = Plan(ops=(SetModulationFormat(lightpath_id="lpAB", mode_id="200G"),))
    report = validate_plan(m, plan, store=_store())
    kinds = {v.type for v in report.violations}
    assert ViolationType.IP_LINK_OVERLOAD in kinds
    v = next(v for v in report.violations if v.type == ViolationType.IP_LINK_OVERLOAD)
    assert v.asset_id == "ipAB"
    assert not v.transient          # overload persists at the committed endpoint
    # Finding 3: IP_LINK_OVERLOAD's overflow-equivalent key is "offload_gbps",
    # distinct from PROTECTION_OVERSUBSCRIBED's "overflow_gbps" (different
    # formula: offered-capacity here vs. working+reserved-capacity there).
    assert v.detail["offload_gbps"] == pytest.approx(100.0)   # 300 offered - 200 cap
    assert "overflow_gbps" not in v.detail


def test_teardown_under_demand_flags_dropped_traffic():
    m = _ip_over_optical(demand=300.0)
    plan = Plan(ops=(TeardownLightpath(lightpath_id="lpAB"),))
    report = validate_plan(m, plan, store=_store(), dropped_tolerance_gbps=0.0)
    kinds = {v.type for v in report.violations}
    assert ViolationType.DROPPED_TRAFFIC in kinds


def test_dropped_traffic_within_tolerance_is_clean():
    m = _ip_over_optical(demand=300.0)
    plan = Plan(ops=(TeardownLightpath(lightpath_id="lpAB"),))
    report = validate_plan(m, plan, store=_store(), dropped_tolerance_gbps=500.0)
    assert ViolationType.DROPPED_TRAFFIC not in {v.type for v in report.violations}


def test_disjointness_collapse_when_working_and_protection_share_oms():
    # working and protection both ride omsAB -> not physically disjoint.
    m = _ip_over_optical(demand=10.0)
    m.add_lightpath(Lightpath(id="lpAB2", oms_sequence=("omsAB",), mode_id="400G",
                              center_freq_hz=193.5e12))
    m.add_ip_link(IPLink(id="ipAB2", a_router="rA", z_router="rB", lightpath_id="lpAB2"))
    m.set_qot_state("lpAB2", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    m._services["svc"] = replace(m.get_service("svc"), protection_path=("ipAB2",))
    report = validate_plan(m, Plan(ops=()), store=_store(),
                           basis="physical", level="link")
    collapse = [v for v in report.violations
                if v.type == ViolationType.DISJOINTNESS_COLLAPSE]
    assert collapse and collapse[0].asset_id == "svc"
    assert "omsAB" in collapse[0].detail["shared_assets"]


def test_disjointness_collapse_via_protection_reroute_op():
    # Same disjointness-collapse setup, but protection_path is set through the
    # RerouteService(which="protection") plan op -- not hand-built via replace() --
    # proving the plan-level path into collapse detection actually works end to end.
    m = _ip_over_optical(demand=10.0)
    m.add_lightpath(Lightpath(id="lpAB2", oms_sequence=("omsAB",), mode_id="400G",
                              center_freq_hz=193.5e12))
    m.add_ip_link(IPLink(id="ipAB2", a_router="rA", z_router="rB", lightpath_id="lpAB2"))
    m.set_qot_state("lpAB2", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    plan = Plan(ops=(RerouteService(service_id="svc", ip_path=("ipAB2",), which="protection"),))
    report = validate_plan(m, plan, store=_store(), basis="physical", level="link")
    collapse = [v for v in report.violations
                if v.type == ViolationType.DISJOINTNESS_COLLAPSE]
    assert collapse and collapse[0].asset_id == "svc"
    assert "omsAB" in collapse[0].detail["shared_assets"]


def test_spectrum_clash_detail_distinguishes_retune_from_reroute():
    # Provision a second lightpath onto lpAB's exact (oms, slot). The clashed
    # state short-circuits QoT (no GNPy), so this is fast and deterministic.
    m = _ip_over_optical(demand=10.0)               # lpAB at 193.4 THz (slot 20) on omsAB
    plan = Plan(ops=(ProvisionLightpath(
        lightpath=Lightpath(id="lpAB2", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12), ip_link=None),))
    report = validate_plan(m, plan, store=_store())
    clash = next(v for v in report.violations
                 if v.type == ViolationType.SPECTRUM_CLASH)
    assert clash.asset_id == "omsAB" and clash.detail["slot"] == 20
    assert set(clash.detail["lightpaths"]) == {"lpAB", "lpAB2"}
    cand = clash.detail["retune_candidates"]["lpAB2"]
    assert cand, "omsAB has free slots -> retune-able (not exhausted)"
    assert 20 not in cand, "the lightpath's own clash slot is excluded"


def test_mode_infeasible_detail_lists_feasible_downshift_modes():
    # Unit-test the helper directly with a seeded negative margin (validate_plan
    # would recompute and overwrite the seed; here we assert the detail contract).
    m = _ip_over_optical()                          # modes 400G(req 10) + 200G(req 7)
    m.set_qot_state("lpAB", QoTState(gsnr_db=8.0, osnr_db=20.0, margin_db=-2.0))
    (t, asset, detail), = _mode_infeasible_findings(m)
    assert t == ViolationType.MODE_INFEASIBLE and asset == "lpAB"
    assert detail["deficit_db"] == 2.0              # 400G needs 10 dB, have 8 dB
    assert "200G" in detail["feasible_downshift_modes"]  # gsnr 8 >= 200G's 7 -> downshift recovers


def test_invalid_plan_surfaces_as_typed_violation_not_exception():
    # teardown of a lightpath that does not exist -> apply_op raises PlanError,
    # which validate_plan must return as a typed INVALID_PLAN, not propagate.
    m = _ip_over_optical()
    plan = Plan(ops=(TeardownLightpath(lightpath_id="ghost"),))
    report = validate_plan(m, plan, store=_store())
    assert not report.ok
    (v,) = report.violations
    assert v.type == ViolationType.INVALID_PLAN
    assert v.asset_id == "ghost"
    assert v.detail["op_index"] == 0
    assert "ghost" in v.detail["message"]


def test_duplicate_provision_is_invalid_plan():
    m = _ip_over_optical()                          # already has lpAB
    plan = Plan(ops=(ProvisionLightpath(
        lightpath=Lightpath(id="lpAB", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.6e12), ip_link=None),))
    report = validate_plan(m, plan, store=_store())
    (v,) = report.violations
    assert v.type == ViolationType.INVALID_PLAN
    assert "lpAB" in v.detail["message"]


def test_unrouted_service_surfaces_as_dropped_traffic():
    m = _ip_over_optical(demand=300.0)
    # a service with demand but NO working path: its demand must not vanish.
    m.add_service(Service(id="ghost_demand", src_router="rA", dst_router="rB",
                          demand_gbps=50.0, working_path=()))
    report = validate_plan(m, Plan(ops=()), store=_store(),
                           dropped_tolerance_gbps=0.0)
    dropped = [v for v in report.violations if v.type == ViolationType.DROPPED_TRAFFIC]
    assert any(v.asset_id == "ghost_demand"
               and v.detail["reason"] == "unrouted" for v in dropped)


def test_protection_path_down_is_not_viable_even_when_disjoint():
    # working on omsAB; protection on a DISJOINT omsCD whose lightpath is dark
    # (margin < 0 -> capacity 0). Disjointness passes; viability must fail.
    m = _ip_over_optical(demand=100.0)
    add_bidir_span(m, "A", "B", "omsCD")
    m.add_lightpath(Lightpath(id="lpCD", oms_sequence=("omsCD",), mode_id="400G",
                              center_freq_hz=193.7e12))
    m.add_ip_link(IPLink(id="ipCD", a_router="rA", z_router="rB", lightpath_id="lpCD"))
    m.set_qot_state("lpCD", QoTState(gsnr_db=5.0, osnr_db=18.0, margin_db=-2.0))  # dark
    m._services["svc"] = replace(m.get_service("svc"), protection_path=("ipCD",))
    (t, asset, detail), = _protection_viability_findings(m)
    assert t == ViolationType.PROTECTION_NOT_VIABLE and asset == "svc"
    assert "ipCD" in detail["dead_links"]
    assert detail["protection_capacity_gbps"] == 0.0
    # disjointness does NOT fire: working omsAB vs protection omsCD are disjoint.
    assert _disjointness_findings(m, "physical", "link") == []


def test_protection_path_undersized_is_not_viable():
    # protection UP but at a capacity below the demand it would inherit on failover.
    m = _ip_over_optical(demand=300.0)
    add_bidir_span(m, "A", "B", "omsCD")
    m.add_lightpath(Lightpath(id="lpCD", oms_sequence=("omsCD",), mode_id="200G",
                              center_freq_hz=193.7e12))           # cap 200 < demand 300
    m.add_ip_link(IPLink(id="ipCD", a_router="rA", z_router="rB", lightpath_id="lpCD"))
    m.set_qot_state("lpCD", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))  # up
    m._services["svc"] = replace(m.get_service("svc"), protection_path=("ipCD",))
    (t, asset, detail), = _protection_viability_findings(m)
    assert t == ViolationType.PROTECTION_NOT_VIABLE and asset == "svc"
    assert detail["dead_links"] == []
    assert detail["bottleneck_link"] == "ipCD"
    assert detail["protection_capacity_gbps"] == 200.0
    assert detail["demand_gbps"] == 300.0


def test_protection_oversubscribed_when_working_plus_reserved_exceeds_cap():
    # ipCD carries svc2's WORKING 300 AND is svc's RESERVED protection 300
    # -> 600 committed on a 400 link -> oversubscribed (1:1 admission failure).
    m = _ip_over_optical(demand=300.0)              # svc on ipAB (working), cap 400
    add_bidir_span(m, "A", "B", "omsCD")
    m.add_lightpath(Lightpath(id="lpCD", oms_sequence=("omsCD",), mode_id="400G",
                              center_freq_hz=193.7e12))
    m.add_ip_link(IPLink(id="ipCD", a_router="rA", z_router="rB", lightpath_id="lpCD"))
    m.set_qot_state("lpCD", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    m._services["svc"] = replace(m.get_service("svc"), protection_path=("ipCD",))
    m.add_service(Service(id="svc2", src_router="rA", dst_router="rB",
                          demand_gbps=300.0, working_path=("ipCD",)))
    (t, link, detail), = _protection_oversubscription_findings(m)
    assert t == ViolationType.PROTECTION_OVERSUBSCRIBED and link == "ipCD"
    assert detail["working_gbps"] == 300.0 and detail["reserved_gbps"] == 300.0
    assert detail["overflow_gbps"] == 200.0
    assert "svc" in detail["reserving_services"]


def test_protection_oversubscribed_does_not_fire_with_zero_reservation():
    # Finding 2: two services both WORKING over ipCD (450 total) exceed its 400
    # cap; no service reserves ipCD as protection (reserved == 0 per
    # reserved_capacity_per_link's real semantics -- it only sums
    # protection_path demand, and nothing here has a protection_path at all).
    # Before the fix, `committed = working + 0 > cap` still fired
    # PROTECTION_OVERSUBSCRIBED, double-reporting a plain capacity overload
    # under the wrong violation type and implying a reservation that doesn't
    # exist.
    m = _ip_over_optical(demand=300.0)               # svc on ipAB (working), cap 400
    # C-D, not A-B: reusing the A-B node pair for a second OMS makes the
    # forward/reverse OMS resolution ambiguous once validate_plan's recompute
    # runs real GNPy (see reverse_oms_sequence) -- an unrelated ambiguity this
    # test must not trip over.
    add_bidir_span(m, "C", "D", "omsCD")
    m.add_lightpath(Lightpath(id="lpCD", oms_sequence=("omsCD",), mode_id="400G",
                              center_freq_hz=193.7e12))
    m.add_ip_link(IPLink(id="ipCD", a_router="rA", z_router="rB", lightpath_id="lpCD"))
    m.set_qot_state("lpCD", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    m.add_service(Service(id="svc2", src_router="rA", dst_router="rB",
                          demand_gbps=300.0, working_path=("ipCD",)))
    m.add_service(Service(id="svc3", src_router="rA", dst_router="rB",
                          demand_gbps=150.0, working_path=("ipCD",)))
    # Sanity: genuinely no reservation anywhere on ipCD.
    assert all("ipCD" not in svc.protection_path for svc in m.list_services())

    assert _protection_oversubscription_findings(m) == [], (
        "reserved==0 on ipCD -> plain capacity overload, not oversubscription"
    )

    report = validate_plan(m, Plan(ops=()), store=_store())
    kinds_on_ipCD = {v.type for v in report.violations if v.asset_id == "ipCD"}
    assert ViolationType.IP_LINK_OVERLOAD in kinds_on_ipCD, (
        "the genuine 450-over-400 overload must still be reported, just under "
        "the correct type"
    )
    assert ViolationType.PROTECTION_OVERSUBSCRIBED not in kinds_on_ipCD


def test_final_state_spectrum_clash_does_not_mistag_persistent_mode_infeasible():
    # Finding 1: state 0 downshifts lpAB into a mode requiring 100 dB GSNR --
    # physically unreachable on an 80 km single span -- so MODE_INFEASIBLE is
    # genuine and nothing in the plan ever un-does it (it persists to the final
    # state too). State 1 ALSO provisions a second lightpath onto lpAB's exact
    # (oms, slot), so the final state's spectrum clash short-circuits its
    # QoT/mode checks entirely. Before the fix, final_keys held only
    # SPECTRUM_CLASH entries, so state 0's MODE_INFEASIBLE read "not in
    # final_keys" and was wrongly tagged transient -- even though the final
    # state's mode check was never actually run, let alone confirmed clear.
    modes = ModeRegistry([
        TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=10.0,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
        TransceiverMode(id="IMPOSSIBLE", bitrate_gbps=1.0, required_gsnr_db=100.0,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    ])
    m = new_model(modes)
    add_bidir_span(m, "A", "B", "omsAB")
    m.add_router(Router(id="rA", site="A"))
    m.add_router(Router(id="rB", site="B"))
    m.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",), mode_id="400G",
                              center_freq_hz=193.4e12))
    m.add_ip_link(IPLink(id="ipAB", a_router="rA", z_router="rB", lightpath_id="lpAB"))
    m.add_service(Service(id="svc", src_router="rA", dst_router="rB",
                          demand_gbps=300.0, working_path=("ipAB",)))
    m.set_qot_state("lpAB", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))

    plan = Plan(ops=(
        SetModulationFormat(lightpath_id="lpAB", mode_id="IMPOSSIBLE"),
        ProvisionLightpath(
            lightpath=Lightpath(id="lpAB2", oms_sequence=("omsAB",), mode_id="400G",
                                center_freq_hz=193.4e12), ip_link=None),
    ))
    report = validate_plan(m, plan, store=_store())
    assert report.num_states == 2
    final_index = report.num_states - 1

    mode_infeasible = [v for v in report.violations
                       if v.type == ViolationType.MODE_INFEASIBLE and v.asset_id == "lpAB"]
    assert mode_infeasible, "the downshift into an unreachable GSNR requirement must be caught"
    assert all(v.state_index == 0 for v in mode_infeasible), (
        "state 1's clash short-circuits its own mode check, so MODE_INFEASIBLE "
        "is only ever recorded at state 0"
    )
    assert all(not v.transient for v in mode_infeasible), (
        "final state's mode check was skipped (clash short-circuit), not "
        "confirmed clear -> must NOT be tagged transient"
    )

    clash = [v for v in report.violations if v.type == ViolationType.SPECTRUM_CLASH]
    assert clash and all(v.state_index == final_index for v in clash)
