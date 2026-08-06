"""Regression for the audit's Critical dangling-IP-link finding: a service
whose working_path/protection_path references a removed IP link (the
documented, valid state left by remove_lightpath/remove_ip_link) must not
crash get_services, get_grooming_map, get_exposure, get_affected_services,
evaluate_objective, or validate_plan (even on an empty plan)."""

from multilayer_optical_mcp.model.assets import Lightpath
from multilayer_optical_mcp.model.ip_assets import Router, IPLink, Service
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.plan import apply_op, TeardownLightpath, service_oms_sequence
from multilayer_optical_mcp.model.views import services_dict
from multilayer_optical_mcp.model.ip_routing import build_grooming_map
from multilayer_optical_mcp.model.exposure import compute_exposure
from multilayer_optical_mcp.model.objective import evaluate_objective
from multilayer_optical_mcp.model.validate import validate_plan
from multilayer_optical_mcp.model.plan import Plan
from tests.phase7_topology import new_model, add_bidir_span


def _model_with_dangling_working_leg() -> NetworkModel:
    """A protected service with TWO lightpaths (working over lp1, protection
    over lp2). Tears down lp1 (the working leg's lightpath) -- the service
    auto-restores onto its protection leg, so it is NOT dropped, but its
    working_path still names the now-removed ip1.

    Built on tests.phase7_topology's synthesizable-topology helpers
    (transceiver-terminated ROADMs + paired reverse OMS) rather than a bare
    hand-rolled fixture: validate_plan's empty-plan path drives a real GNPy
    recompute, and gated_qot always evaluates both forward and backward
    directions, which (per phase7_topology's module docstring, S3-11/S4-2/
    S4-3) requires transceiver-backed ROADM endpoints and a paired reverse
    OMS -- a bare amp+fiber OMS with no transceiver raises a GNPy
    NetworkTopologyError unrelated to this task's dangling-ip-link fix. The
    working (oms1: A->B) and protection (oms2: A->C) legs use DISTINCT node
    pairs -- two parallel forward OMS sharing one node pair make reverse-OMS
    resolution ambiguous (translate.py's per-OMS disambiguator has no way to
    attribute a shared reverse candidate to one of several forward siblings).
    """
    n = new_model()
    add_bidir_span(n, "A", "B", "oms1")
    add_bidir_span(n, "A", "C", "oms2")
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id="400G", center_freq_hz=193.4e12))
    n.add_lightpath(Lightpath(id="lp2", oms_sequence=("oms2",),
                              mode_id="400G", center_freq_hz=193.5e12))
    n.set_qot_state("lp1", QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=8.0))
    n.set_qot_state("lp2", QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=8.0))
    n.add_router(Router(id="R-A", site="A"))
    n.add_router(Router(id="R-B", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R-A", z_router="R-B", lightpath_id="lp1"))
    n.add_ip_link(IPLink(id="ip2", a_router="R-A", z_router="R-B", lightpath_id="lp2"))
    n.add_service(Service(id="svc", src_router="R-A", dst_router="R-B",
                          demand_gbps=50.0, working_path=("ip1",),
                          protection_path=("ip2",)))
    apply_op(n, TeardownLightpath(lightpath_id="lp1"))
    return n


def test_get_ip_link_lightpath_id_returns_none_for_dangling_id():
    n = _model_with_dangling_working_leg()
    assert n.get_ip_link_lightpath_id("ip1") is None
    assert n.get_ip_link_lightpath_id("ip2") == "lp2"


def test_service_oms_sequence_skips_dangling_link():
    n = _model_with_dangling_working_leg()
    svc = n.get_service("svc")
    assert service_oms_sequence(n, svc.working_path) == ()
    assert service_oms_sequence(n, svc.protection_path) == ("oms2",)


def test_services_dict_does_not_crash():
    n = _model_with_dangling_working_leg()
    out = services_dict(n)
    assert out["services"][0]["id"] == "svc"


def test_build_grooming_map_does_not_crash():
    n = _model_with_dangling_working_leg()
    gm = build_grooming_map(n)
    assert gm.by_service["svc"] == ()   # dangling link contributes no lightpath


def test_get_exposure_does_not_crash():
    n = _model_with_dangling_working_leg()
    n.define_risk_group("rg1", ("oms2",))
    res = compute_exposure(n, "svc", "rg1")
    assert res.protection_intersects is True


def test_evaluate_objective_does_not_crash():
    n = _model_with_dangling_working_leg()
    res = evaluate_objective(n)
    assert res.added_latency >= 0.0


def test_validate_plan_empty_plan_does_not_crash():
    n = _model_with_dangling_working_leg()
    store = QoTResultStore()
    report = validate_plan(n, Plan(ops=()), store=store)
    assert report.num_states == 1
