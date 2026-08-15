import pytest
from multilayer_optical_mcp.server import build_app
from multilayer_optical_network.model.assets import FiberType, Amplifier, Fiber, OMS, ROADM, Lightpath
from multilayer_optical_network.model.ip_assets import Router, IPLink, Service
from multilayer_optical_network.model.qot import QoTState
from tests.conftest import call_tool


def _seed(app):
    """Populate the app's current model with the A->B->C two-link network."""
    n = app._snapshots.current()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for nid in ("a1", "a2", "a3"):
        n.add_amplifier(Amplifier(id=nid, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0,
                      type_variety="SSMF"))
    n.add_fiber(Fiber(id="fBC", a_end="a2", z_end="a3", length_km=80.0,
                      type_variety="SSMF"))
    for node in ("A", "B", "C"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "a1", "fAB", "a2")))
    n.add_oms(OMS(id="omsBC", src_node_id="B", dst_node_id="C",
                  elements=("roadm_B", "a2", "fBC", "a3")))
    # modes come from modulation_formats.yaml; pick the first available id.
    mode_id = app._snapshots.current().modes.list()[0].id
    n.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",),
                              mode_id=mode_id, center_freq_hz=193.4e12))
    n.add_lightpath(Lightpath(id="lpBC", oms_sequence=("omsBC",),
                              mode_id=mode_id, center_freq_hz=193.4e12))
    for rid, site in (("R-A", "A"), ("R-B", "B"), ("R-C", "C")):
        n.add_router(Router(id=rid, site=site))
    n.add_ip_link(IPLink(id="ipAB", a_router="R-A", z_router="R-B",
                         lightpath_id="lpAB"))
    n.add_ip_link(IPLink(id="ipBC", a_router="R-B", z_router="R-C",
                         lightpath_id="lpBC"))
    bitrate = n.modes.list()[0].bitrate_gbps
    n.set_qot_state("lpAB", QoTState(gsnr_db=30.0, osnr_db=32.0, margin_db=5.0))
    n.set_qot_state("lpBC", QoTState(gsnr_db=30.0, osnr_db=32.0, margin_db=5.0))
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=bitrate * 0.5, working_path=("ipAB", "ipBC")))
    return bitrate


def test_get_ip_topology_annotates_links():
    app = build_app()
    bitrate = _seed(app)
    d = call_tool(app, "get_ip_topology")
    links = {link["id"]: link for link in d["ip_links"]}
    assert links["ipAB"]["capacity_gbps"] == bitrate
    assert links["ipAB"]["load_gbps"] == pytest.approx(bitrate * 0.5)


def test_get_grooming_map_tool():
    app = build_app()
    _seed(app)
    d = call_tool(app, "get_grooming_map")
    assert d["by_service"]["svc-AC"] == ["lpAB", "lpBC"]


def test_get_affected_services_tool():
    app = build_app()
    _seed(app)
    assert call_tool(app, "get_affected_services", asset_id="lpBC")["services"] == ["svc-AC"]


def test_simulate_ip_routing_tool():
    app = build_app()
    _seed(app)
    d = call_tool(app, "simulate_ip_routing")
    assert set(d) == {"utilizations", "congestion", "restored", "dropped"}
    assert d["congestion"] == []


def test_reroute_service_tool_repins_and_resimulates():
    app = build_app()
    bitrate = _seed(app)
    n = app._snapshots.current()
    # Add a direct express link A->C to reroute onto.
    mode_id = n.modes.list()[0].id
    n.add_lightpath(Lightpath(id="lpAC", oms_sequence=("omsAB", "omsBC"),
                              mode_id=mode_id, center_freq_hz=193.5e12))
    n.set_qot_state("lpAC", QoTState(gsnr_db=30.0, osnr_db=32.0, margin_db=5.0))
    n.add_ip_link(IPLink(id="ipAC", a_router="R-A", z_router="R-C",
                         lightpath_id="lpAC"))
    out = call_tool(app, "reroute_service", service_id="svc-AC", ip_path=["ipAC"])
    assert out["service_id"] == "svc-AC"
    assert out["working_path"] == ["ipAC"]
    # Load moved: ipAB now idle, ipAC carries the demand.
    topo = {link["id"]: link for link in call_tool(app, "get_ip_topology")["ip_links"]}
    assert topo["ipAB"]["load_gbps"] == 0.0
    assert topo["ipAC"]["load_gbps"] == pytest.approx(bitrate * 0.5)


def test_reroute_service_tool_rejects_bad_path():
    app = build_app()
    _seed(app)
    with pytest.raises(ValueError, match="does not connect"):
        call_tool(app, "reroute_service", service_id="svc-AC", ip_path=["ipAB"])


def test_reroute_service_tool_which_protection_returns_protection_path():
    app = build_app()
    _seed(app)
    n = app._snapshots.current()
    mode_id = n.modes.list()[0].id
    n.add_lightpath(Lightpath(id="lpAC", oms_sequence=("omsAB", "omsBC"),
                              mode_id=mode_id, center_freq_hz=193.5e12))
    n.set_qot_state("lpAC", QoTState(gsnr_db=30.0, osnr_db=32.0, margin_db=5.0))
    n.add_ip_link(IPLink(id="ipAC", a_router="R-A", z_router="R-C",
                         lightpath_id="lpAC"))
    out = call_tool(app, "reroute_service", service_id="svc-AC", ip_path=["ipAC"],
               which="protection")
    assert out["service_id"] == "svc-AC"
    assert out["protection_path"] == ["ipAC"]
    assert "working_path" not in out

    # omitted `which` still targets working_path (regression guard), unaffected by
    # the protection reroute above:
    out2 = call_tool(app, "reroute_service", service_id="svc-AC", ip_path=["ipAC"])
    assert out2["working_path"] == ["ipAC"]
    assert "protection_path" not in out2
