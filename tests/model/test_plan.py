import pytest
from multilayer_optical_mcp.model.assets import ROADM
from multilayer_optical_mcp.model.assets import FiberType, Amplifier, Fiber, OMS, Lightpath
from multilayer_optical_mcp.model.ip_assets import IPLink, Service, Router
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.plan import (
    Plan, ProvisionLightpath, TeardownLightpath, RerouteService,
    SetModulationFormat, apply_op, replay, plan_from_dict, service_oms_sequence,
)

MODES = ModeRegistry([
    TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=10.0,
                    symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    TransceiverMode(id="200G", bitrate_gbps=200.0, required_gsnr_db=7.0,
                    symbol_rate_baud=43.75e9, channel_spacing_hz=100e9),
])


def _model():
    m = NetworkModel(modes=MODES)
    m.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    m.add_amplifier(Amplifier(id="a1", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    for node in ("A", "B"):
        m.add_roadm(ROADM(id=f"roadm_{node}"))
    m.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B", elements=("roadm_A", "a1", "fAB")))
    m.add_router(Router(id="rA", site="A"))
    m.add_router(Router(id="rB", site="B"))
    return m


def test_provision_adds_lightpath_and_binds_ip_link():
    m = _model()
    op = ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12),
        ip_link=IPLink(id="ip1", a_router="rA", z_router="rB", lightpath_id="lp1"))
    apply_op(m, op)
    assert "lp1" in m._lightpaths
    assert m.get_ip_link("ip1").lightpath_id == "lp1"


def test_teardown_removes_lightpath_and_ip_link():
    m = _model()
    apply_op(m, ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12),
        ip_link=IPLink(id="ip1", a_router="rA", z_router="rB", lightpath_id="lp1")))
    apply_op(m, TeardownLightpath(lightpath_id="lp1"))
    assert "lp1" not in m._lightpaths
    assert "ip1" not in m._ip_links


def test_set_modulation_format_changes_mode():
    m = _model()
    apply_op(m, ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12), ip_link=None))
    apply_op(m, SetModulationFormat(lightpath_id="lp1", mode_id="200G"))
    assert m.get_lightpath("lp1").mode_id == "200G"


def test_replay_applies_all_ops_in_order_on_a_clone():
    m = _model()
    plan = Plan(ops=(
        ProvisionLightpath(
            lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                                center_freq_hz=193.4e12),
            ip_link=IPLink(id="ip1", a_router="rA", z_router="rB", lightpath_id="lp1")),
        SetModulationFormat(lightpath_id="lp1", mode_id="200G"),
    ))
    out = replay(m, plan)
    assert out.get_lightpath("lp1").mode_id == "200G"
    assert "lp1" not in m._lightpaths       # replay never touches the input model


def test_plan_from_dict_round_trips_each_op():
    plan = plan_from_dict({"ops": [
        {"op": "provision_lightpath",
         "lightpath": {"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": "400G",
                       "center_freq_hz": 193.4e12},
         "ip_link": {"id": "ip1", "a_router": "rA", "z_router": "rB"}},
        {"op": "teardown_lightpath", "lightpath_id": "lp1"},
        {"op": "reroute_service", "service_id": "svc", "ip_path": ["ip1"]},
        {"op": "set_modulation_format", "lightpath_id": "lp1", "mode_id": "200G"},
    ]})
    assert isinstance(plan.ops[0], ProvisionLightpath)
    assert plan.ops[0].ip_link.lightpath_id == "lp1"   # bound to the new lightpath
    assert isinstance(plan.ops[1], TeardownLightpath)
    assert isinstance(plan.ops[2], RerouteService)
    assert isinstance(plan.ops[3], SetModulationFormat)


def test_service_oms_sequence_traces_ip_path_to_oms():
    m = _model()
    apply_op(m, ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12),
        ip_link=IPLink(id="ip1", a_router="rA", z_router="rB", lightpath_id="lp1")))
    assert service_oms_sequence(m, ("ip1",)) == ("omsAB",)


def test_provision_duplicate_lightpath_id_raises_plan_error():
    from multilayer_optical_mcp.model.plan import PlanError
    m = _model()
    op = ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12), ip_link=None)
    apply_op(m, op)
    with pytest.raises(PlanError):
        apply_op(m, op)        # same lightpath id again -> rejected, not overwritten


def _model_with_service():
    m = _model()
    apply_op(m, ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12),
        ip_link=IPLink(id="ip1", a_router="rA", z_router="rB", lightpath_id="lp1")))
    m.add_service(Service(id="svc", src_router="rA", dst_router="rB",
                          demand_gbps=50.0, working_path=("ip1",)))
    return m


def test_reroute_service_which_protection_dispatches_to_protection_path():
    m = _model_with_service()
    apply_op(m, RerouteService(service_id="svc", ip_path=("ip1",), which="protection"))
    svc = m.get_service("svc")
    assert svc.protection_path == ("ip1",)
    assert svc.working_path == ("ip1",)               # untouched (fixture already set it)


def test_reroute_service_which_default_is_working_regression_guard():
    m = _model_with_service()
    apply_op(m, RerouteService(service_id="svc", ip_path=("ip1",)))   # which omitted
    svc = m.get_service("svc")
    assert svc.working_path == ("ip1",)
    assert svc.protection_path == ()                  # unchanged, still empty


def test_reroute_service_unrecognized_which_raises_plan_error():
    from multilayer_optical_mcp.model.plan import PlanError
    m = _model_with_service()
    with pytest.raises(PlanError, match="which"):
        apply_op(m, RerouteService(service_id="svc", ip_path=("ip1",), which="bogus"))


def test_plan_from_dict_parses_which_key():
    plan = plan_from_dict({"ops": [
        {"op": "reroute_service", "service_id": "svc", "ip_path": ["ip1"], "which": "protection"},
    ]})
    assert plan.ops[0].which == "protection"


def test_plan_from_dict_defaults_which_to_working_when_absent():
    plan = plan_from_dict({"ops": [
        {"op": "reroute_service", "service_id": "svc", "ip_path": ["ip1"]},
    ]})
    assert plan.ops[0].which == "working"
