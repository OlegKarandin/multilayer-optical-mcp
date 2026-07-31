import pytest
from multilayer_optical_mcp.model.assets import (
    Fiber, FiberType, Amplifier, Lightpath, Router, IPLink, OMS, ROADM,
    Transceiver, TransceiverMode, Service, SRLG,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel


def _registry():
    return ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ])


def _bare():
    return NetworkModel(modes=_registry())


def test_fiber_requires_registered_type():
    n = _bare()
    with pytest.raises(ValueError, match="unknown fiber type"):
        n.add_fiber(Fiber(id="f1", a_end="A", z_end="B",
                          length_km=80.0, type_variety="SSMF"))


def test_fiber_accepted_after_type_registered():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    f = Fiber(id="f1", a_end="A", z_end="B", length_km=80.0, type_variety="SSMF")
    n.add_fiber(f)
    assert n.get_fiber("f1") is f
    assert n.get_fiber_type("SSMF").loss_coef_db_per_km == 0.2


def test_oms_requires_existing_elements():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    with pytest.raises(ValueError, match="neither fiber, amplifier, nor roadm"):
        n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                      elements=("amp1", "fiber-missing")))


def test_optical_node_shadow_registry_is_gone():
    # S1-6: the OpticalNode class and its _optical_nodes registry were written,
    # cloned, and never read. Lock the removal so nobody resurrects a second,
    # perpetually-drifting node store alongside _roadms/_transceivers.
    import multilayer_optical_mcp.model.assets as assets
    assert not hasattr(assets, "OpticalNode")
    n = _bare()
    assert not hasattr(n, "add_optical_node")
    assert not hasattr(n, "_optical_nodes")
    assert not hasattr(n.clone(), "_optical_nodes")


def test_lightpath_requires_existing_oms():
    n = _bare()
    with pytest.raises(ValueError, match="unknown OMS"):
        n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms-missing",),
                                  mode_id="100G-QPSK", center_freq_hz=193.4e12))


def test_ip_link_requires_existing_lightpath():
    n = _bare()
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    with pytest.raises(ValueError, match="unknown lightpath"):
        n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                             lightpath_id="lp-missing"))


def test_full_happy_path_add_chain():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="trxA", dst_node_id="trxB",
                  elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    assert n.get_ip_link("ip1").lightpath_id == "lp1"
    assert n.get_oms("oms1").elements == ("amp1", "f1", "amp2")


def _model_with_service():
    """A->B IP topology (ipAB) plus a service A->B with only working_path set."""
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B", elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",), mode_id="100G-QPSK",
                              center_freq_hz=193.4e12))
    n.add_router(Router(id="R-A", site="A"))
    n.add_router(Router(id="R-B", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R-A", z_router="R-B", lightpath_id="lp1"))
    n.add_service(Service(id="svc", src_router="R-A", dst_router="R-B",
                          demand_gbps=50.0, working_path=("ip1",)))
    return n


def test_set_service_protection_path_replaces_protection_leaves_working():
    n = _model_with_service()
    n.set_service_protection_path("svc", ("ip1",))
    svc = n.get_service("svc")
    assert svc.protection_path == ("ip1",)
    assert svc.working_path == ("ip1",)               # untouched


def test_set_service_protection_path_rejects_unknown_ip_link():
    n = _model_with_service()
    with pytest.raises(ValueError, match="unknown IP link"):
        n.set_service_protection_path("svc", ("ip-missing",))


def test_set_service_protection_path_rejects_non_contiguous_path():
    n = _model_with_service()
    # Second link B->C: does not connect src_router="R-A" to dst_router="R-B".
    n.add_lightpath(Lightpath(id="lp2", oms_sequence=("oms1",), mode_id="100G-QPSK",
                              center_freq_hz=193.5e12))
    n.add_router(Router(id="R-C", site="C"))
    n.add_ip_link(IPLink(id="ip2", a_router="R-B", z_router="R-C", lightpath_id="lp2"))
    with pytest.raises(ValueError, match="does not connect"):
        n.set_service_protection_path("svc", ("ip2",))


def test_add_service_rejects_duplicate_id():
    """Regression for the audit's Important finding: add_service must not
    silently overwrite an existing same-id service's paths."""
    n = _model_with_service()   # already defines service "svc" with working_path=("ip1",)
    with pytest.raises(ValueError, match="already exists"):
        n.add_service(Service(id="svc", src_router="R-A", dst_router="R-B",
                              demand_gbps=999.0, working_path=()))
    # The original service must be untouched.
    assert n.get_service("svc").working_path == ("ip1",)


# ---------------------------------------------------------------- Task 1: finding 1
# add_ip_link must validate a_router/z_router the same way it already
# validates lightpath_id, instead of letting a typo'd router id become a
# dangling reference that only surfaces later in get_ip_topology/routing.

def test_add_ip_link_rejects_unknown_a_router():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B", elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",), mode_id="100G-QPSK",
                              center_freq_hz=193.4e12))
    n.add_router(Router(id="R2", site="B"))
    with pytest.raises(ValueError, match="unknown a_router"):
        n.add_ip_link(IPLink(id="ip1", a_router="R-missing", z_router="R2", lightpath_id="lp1"))
    assert n.list_ip_links() == ()


def test_add_ip_link_rejects_unknown_z_router():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B", elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",), mode_id="100G-QPSK",
                              center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    with pytest.raises(ValueError, match="unknown z_router"):
        n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R-missing", lightpath_id="lp1"))
    assert n.list_ip_links() == ()


# ---------------------------------------------------------------- Task 1: finding 2
# add_service must reject a non-contiguous path at creation time, not just on
# the later set_service_working_path/set_service_protection_path setters.

def test_add_service_rejects_non_contiguous_working_path():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B", elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",), mode_id="100G-QPSK",
                              center_freq_hz=193.4e12))
    n.add_router(Router(id="R-A", site="A"))
    n.add_router(Router(id="R-B", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R-A", z_router="R-B", lightpath_id="lp1"))
    # ip1 connects R-A<->R-B, but the service is declared R-A -> R-C.
    n.add_router(Router(id="R-C", site="C"))
    with pytest.raises(ValueError, match="does not connect"):
        n.add_service(Service(id="svc", src_router="R-A", dst_router="R-C",
                              demand_gbps=50.0, working_path=("ip1",)))
    assert n.list_services() == ()


def test_add_service_rejects_non_contiguous_protection_path():
    n = _model_with_service()   # svc: R-A -> R-B, working_path=("ip1",)
    # Second link B->C: does not connect R-A -> R-B.
    n.add_lightpath(Lightpath(id="lp2", oms_sequence=("oms1",), mode_id="100G-QPSK",
                              center_freq_hz=193.5e12))
    n.add_router(Router(id="R-C", site="C"))
    n.add_ip_link(IPLink(id="ip2", a_router="R-B", z_router="R-C", lightpath_id="lp2"))
    with pytest.raises(ValueError, match="does not connect"):
        n.add_service(Service(id="svc2", src_router="R-A", dst_router="R-B",
                              demand_gbps=10.0, protection_path=("ip2",)))
    assert "svc2" not in {s.id for s in n.list_services()}


def test_add_service_accepts_empty_paths():
    """Unchanged behavior: a service with no path yet (not routed) is legal
    and must not trip the new contiguity check."""
    n = _bare()
    n.add_router(Router(id="R-A", site="A"))
    n.add_router(Router(id="R-B", site="B"))
    n.add_service(Service(id="svc", src_router="R-A", dst_router="R-B", demand_gbps=10.0))
    assert n.get_service("svc").working_path == ()


# ---------------------------------------------------------------- Task 1: finding 4
# Every add_* registry (except add_service/define_risk_group, already guarded)
# must reject a duplicate id rather than silently overwriting the existing
# entry -- same footgun class, same fix pattern, across the remaining 9
# registries.

def _seeded_full_model() -> NetworkModel:
    """One of everything, so each duplicate-id test can re-add with the same
    id but different content and assert the original survives untouched."""
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_roadm(ROADM(id="roadm_A"))
    n.add_transceiver(Transceiver(id="trx_A", site="A"))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B", elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",), mode_id="100G-QPSK",
                              center_freq_hz=193.4e12))
    n.add_router(Router(id="R-A", site="A"))
    n.add_router(Router(id="R-B", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R-A", z_router="R-B", lightpath_id="lp1"))
    n.add_srlg(SRLG(id="srlg1", asset_ids=("f1",)))
    return n


def test_add_fiber_rejects_duplicate_id():
    n = _seeded_full_model()
    with pytest.raises(ValueError, match="already exists"):
        n.add_fiber(Fiber(id="f1", a_end="X", z_end="Y", length_km=1.0, type_variety="SSMF"))
    assert n.get_fiber("f1").length_km == 80.0


def test_add_amplifier_rejects_duplicate_id():
    n = _seeded_full_model()
    with pytest.raises(ValueError, match="already exists"):
        n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy", gain_db=1.0, nf_db=1.0))
    assert n.get_amplifier("amp1").gain_db == 20.0


def test_add_roadm_rejects_duplicate_id():
    n = _seeded_full_model()
    with pytest.raises(ValueError, match="already exists"):
        n.add_roadm(ROADM(id="roadm_A", target_pch_out_db=-99.0))
    assert n.has_roadm("roadm_A")


def test_add_transceiver_rejects_duplicate_id():
    n = _seeded_full_model()
    with pytest.raises(ValueError, match="already exists"):
        n.add_transceiver(Transceiver(id="trx_A", site="Z"))
    # No public getter for transceivers; reach into the registry directly to
    # confirm the original entry (site="A") survives, not overwritten.
    assert n._transceivers["trx_A"].site == "A"


def test_add_oms_rejects_duplicate_id():
    n = _seeded_full_model()
    with pytest.raises(ValueError, match="already exists"):
        n.add_oms(OMS(id="oms1", src_node_id="X", dst_node_id="Y", elements=("amp1",)))
    assert n.get_oms("oms1").elements == ("amp1", "f1", "amp2")


def test_add_lightpath_rejects_duplicate_id():
    n = _seeded_full_model()
    with pytest.raises(ValueError, match="already exists"):
        n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",), mode_id="100G-QPSK",
                                  center_freq_hz=195.0e12))
    assert n.get_lightpath("lp1").center_freq_hz == 193.4e12


def test_add_ip_link_rejects_duplicate_id():
    n = _seeded_full_model()
    with pytest.raises(ValueError, match="already exists"):
        n.add_ip_link(IPLink(id="ip1", a_router="R-B", z_router="R-A", lightpath_id="lp1"))
    assert n.get_ip_link("ip1").a_router == "R-A"


def test_add_router_rejects_duplicate_id():
    n = _seeded_full_model()
    with pytest.raises(ValueError, match="already exists"):
        n.add_router(Router(id="R-A", site="ELSEWHERE"))
    assert n.get_router("R-A").site == "A"


def test_add_srlg_rejects_duplicate_id():
    n = _seeded_full_model()
    with pytest.raises(ValueError, match="already exists"):
        n.add_srlg(SRLG(id="srlg1", asset_ids=("amp1",)))
    assert n.get_srlg_members("srlg1") == ("f1",)
