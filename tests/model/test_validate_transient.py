"""The headline Phase 7 property: validate_plan checks EVERY intermediate state,
not just the endpoints. A plan whose endpoints are both clean can still be unsafe
between them — break-before-make drops a service transiently; reordering to
make-before-break clears it. Deterministic and physics-free (the drop comes from
sequencing, not from a marginal QoT — both spans carry comfortably-positive margin).
"""
from multilayer_optical_mcp.model.assets import Lightpath, IPLink, Service
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.plan import (
    Plan, ProvisionLightpath, RerouteService, TeardownLightpath,
)
from multilayer_optical_mcp.model.validate import validate_plan, ViolationType
from tests.phase7_topology import new_model, add_bidir_span


def _two_path_model():
    """svc rA->rB rides ipOld (lpOld/omsAB). A parallel ipNew (lpNew/omsAB2) is
    standing by. Migration moves svc from ipOld to ipNew, then tears down lpOld."""
    m = new_model()
    add_bidir_span(m, "A", "B", "omsAB")
    add_bidir_span(m, "A", "B", "omsAB2")
    m.add_lightpath(Lightpath(id="lpOld", oms_sequence=("omsAB",), mode_id="400G",
                              center_freq_hz=193.4e12))
    m.add_lightpath(Lightpath(id="lpNew", oms_sequence=("omsAB2",), mode_id="400G",
                              center_freq_hz=193.4e12))
    m.add_ip_link(IPLink(id="ipOld", a_router="rA", z_router="rB", lightpath_id="lpOld"))
    m.add_ip_link(IPLink(id="ipNew", a_router="rA", z_router="rB", lightpath_id="lpNew"))
    m.add_service(Service(id="svc", src_router="rA", dst_router="rB",
                          demand_gbps=300.0, working_path=("ipOld",)))
    m.set_qot_state("lpOld", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    m.set_qot_state("lpNew", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    return m


def _store():
    return QoTResultStore()


def test_break_before_make_drops_service_transiently():
    # WRONG order: teardown lpOld first (svc still on ipOld -> dropped at state 0),
    # then reroute onto ipNew (clean at the final state).
    m = _two_path_model()
    plan = Plan(ops=(
        TeardownLightpath(lightpath_id="lpOld"),
        RerouteService(service_id="svc", ip_path=("ipNew",)),
    ))
    report = validate_plan(m, plan, store=_store())
    drops = [v for v in report.violations if v.type == ViolationType.DROPPED_TRAFFIC]
    assert drops, "intermediate-state drop must be caught"
    assert any(v.transient and v.state_index == 0 for v in drops), \
        "the drop exists mid-sequence and is gone at the endpoint -> transient"


def test_make_before_break_is_clean():
    # RIGHT order: reroute onto ipNew first, then teardown lpOld. No state drops.
    m = _two_path_model()
    plan = Plan(ops=(
        RerouteService(service_id="svc", ip_path=("ipNew",)),
        TeardownLightpath(lightpath_id="lpOld"),
    ))
    report = validate_plan(m, plan, store=_store())
    assert ViolationType.DROPPED_TRAFFIC not in {v.type for v in report.violations}
    assert report.ok


def test_endpoint_only_check_would_miss_the_transient():
    # Sanity: the final state of the WRONG-order plan is itself clean — proving the
    # violation is only visible because validate_plan checks intermediate states.
    m = _two_path_model()
    bad = Plan(ops=(
        TeardownLightpath(lightpath_id="lpOld"),
        RerouteService(service_id="svc", ip_path=("ipNew",)),
    ))
    report = validate_plan(m, bad, store=_store())
    final_index = report.num_states - 1
    final_drops = [v for v in report.violations
                   if v.type == ViolationType.DROPPED_TRAFFIC
                   and v.state_index == final_index]
    assert final_drops == []      # endpoint clean; only the transient state caught it


def _migration_model():
    """svc rides ipOld (lpOld/omsAB). A parallel span omsAB2 is built and ready,
    but lpNew is NOT yet provisioned — the 3-op make-before-break will provision
    it, migrate onto it, then tear down lpOld."""
    m = new_model()
    add_bidir_span(m, "A", "B", "omsAB")
    add_bidir_span(m, "A", "B", "omsAB2")
    m.add_lightpath(Lightpath(id="lpOld", oms_sequence=("omsAB",), mode_id="400G",
                              center_freq_hz=193.4e12))
    m.add_ip_link(IPLink(id="ipOld", a_router="rA", z_router="rB", lightpath_id="lpOld"))
    m.add_service(Service(id="svc", src_router="rA", dst_router="rB",
                          demand_gbps=300.0, working_path=("ipOld",)))
    m.set_qot_state("lpOld", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    return m


def _new_lp_ops():
    return (
        ProvisionLightpath(
            lightpath=Lightpath(id="lpNew", oms_sequence=("omsAB2",), mode_id="400G",
                                center_freq_hz=193.4e12),
            ip_link=IPLink(id="ipNew", a_router="rA", z_router="rB", lightpath_id="lpNew")),
    )


def test_three_state_make_before_break_composes_clean():
    # Canonical 3-op MBB: provision new, reroute onto it, THEN tear down old.
    # Every intermediate state stays up (at state 0 both lpOld and lpNew are lit —
    # the old-union-new overlap the arbitrary-loading contract must evaluate).
    m = _migration_model()
    plan = Plan(ops=(
        *_new_lp_ops(),                                   # state 0: lpOld + lpNew both lit
        RerouteService(service_id="svc", ip_path=("ipNew",)),  # state 1: svc on ipNew
        TeardownLightpath(lightpath_id="lpOld"),          # state 2: old gone
    ))
    report = validate_plan(m, plan, store=_store())
    assert report.num_states == 3
    assert report.ok, [ (v.type, v.state_index) for v in report.violations ]


def test_three_state_break_before_make_is_caught_transient():
    # WRONG 3-op order: teardown old FIRST (svc still on ipOld -> dropped at state 0),
    # provision new, reroute. Endpoint clean, but the mid-sequence drop is flagged
    # transient — proving intermediate-state checking composes over 3 states.
    m = _migration_model()
    plan = Plan(ops=(
        TeardownLightpath(lightpath_id="lpOld"),          # state 0: svc dropped
        *_new_lp_ops(),                                   # state 1
        RerouteService(service_id="svc", ip_path=("ipNew",)),  # state 2: recovered
    ))
    report = validate_plan(m, plan, store=_store())
    drops = [v for v in report.violations if v.type == ViolationType.DROPPED_TRAFFIC]
    assert any(v.transient and v.state_index == 0 for v in drops)
    final_index = report.num_states - 1
    assert not any(v.type == ViolationType.DROPPED_TRAFFIC
                   and v.state_index == final_index for v in report.violations)
