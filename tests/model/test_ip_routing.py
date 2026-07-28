# tests/model/test_ip_routing.py
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, Lightpath, Router, IPLink, Service,
    TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState


def _two_link_model():
    """A→B→C: two IP links, each bound to its own single-OMS lightpath."""
    reg = ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
        TransceiverMode(id="200G-16QAM", bitrate_gbps=200.0, required_gsnr_db=18.5,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
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
    n.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",),
                              mode_id="200G-16QAM", center_freq_hz=193.4e12))
    n.add_lightpath(Lightpath(id="lpBC", oms_sequence=("omsBC",),
                              mode_id="200G-16QAM", center_freq_hz=193.4e12))
    for rid, site in (("R-A", "A"), ("R-B", "B"), ("R-C", "C")):
        n.add_router(Router(id=rid, site=site))
    n.add_ip_link(IPLink(id="ipAB", a_router="R-A", z_router="R-B",
                         lightpath_id="lpAB"))
    n.add_ip_link(IPLink(id="ipBC", a_router="R-B", z_router="R-C",
                         lightpath_id="lpBC"))
    # Healthy QoT on both lightpaths (200G feasible, margin positive).
    n.set_qot_state("lpAB", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=3.5))
    n.set_qot_state("lpBC", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=3.5))
    return n


def test_ip_links_for_lightpath_reverse_lookup():
    n = _two_link_model()
    assert n.ip_links_for_lightpath("lpAB") == ("ipAB",)
    assert n.ip_links_for_lightpath("lpBC") == ("ipBC",)


def test_ip_links_for_lightpath_unknown_raises():
    n = _two_link_model()
    with pytest.raises(KeyError):
        n.ip_links_for_lightpath("nope")


def test_grooming_map_both_directions():
    from multilayer_optical_mcp.model import ip_routing
    n = _two_link_model()
    # One service A->C rides both lightpaths; one service A->B rides only lpAB.
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    n.add_service(Service(id="svc-AB", src_router="R-A", dst_router="R-B",
                          demand_gbps=40.0, working_path=("ipAB",)))
    gm = ip_routing.build_grooming_map(n)
    # by_service: each service -> the lightpaths along its working_path
    assert gm.by_service["svc-AC"] == ("lpAB", "lpBC")
    assert gm.by_service["svc-AB"] == ("lpAB",)
    # by_lightpath: each lightpath -> the services riding it (sorted)
    assert gm.by_lightpath["lpAB"] == ("svc-AB", "svc-AC")
    assert gm.by_lightpath["lpBC"] == ("svc-AC",)


def test_simulate_offered_load_and_utilization():
    from multilayer_optical_mcp.model import ip_routing
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    n.add_service(Service(id="svc-AB", src_router="R-A", dst_router="R-B",
                          demand_gbps=40.0, working_path=("ipAB",)))
    res = ip_routing.simulate_ip_routing(n)
    util = {u.ip_link_id: u for u in res.utilizations}
    # ipAB carries 120 + 40 = 160 of 200 -> util 0.8; ipBC carries 120 -> 0.6
    assert util["ipAB"].offered_gbps == 160.0
    assert util["ipAB"].capacity_gbps == 200.0
    assert util["ipAB"].utilization == pytest.approx(0.8)
    assert util["ipBC"].utilization == pytest.approx(0.6)
    assert res.congested_links == ()
    assert res.down_links == ()
    assert res.dropped_services == ()
    assert res.overflow_gbps == 0.0


def test_simulate_unrouted_service_carries_no_load():
    from multilayer_optical_mcp.model import ip_routing
    n = _two_link_model()
    n.add_service(Service(id="svc-pending", src_router="R-A", dst_router="R-C",
                          demand_gbps=999.0))  # empty working_path
    res = ip_routing.simulate_ip_routing(n)
    assert all(u.offered_gbps == 0.0 for u in res.utilizations)


def test_simulate_oversubscription_reports_overflow():
    from multilayer_optical_mcp.model import ip_routing
    n = _two_link_model()
    # 260G offered onto a 200G link -> congested, overflow 60, not down.
    n.add_service(Service(id="svc-big", src_router="R-A", dst_router="R-B",
                          demand_gbps=260.0, working_path=("ipAB",)))
    res = ip_routing.simulate_ip_routing(n)
    util = {u.ip_link_id: u for u in res.utilizations}
    assert util["ipAB"].utilization == pytest.approx(1.3)
    assert res.congested_links == ("ipAB",)
    assert res.down_links == ()
    assert res.overflow_gbps == pytest.approx(60.0)
    assert res.dropped_services == ()  # not down: congested, not lost


def test_simulate_down_link_drops_its_services():
    from multilayer_optical_mcp.model import ip_routing
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    # Push lpBC margin negative -> capacity 0 -> ipBC down.
    n.set_qot_state("lpBC", QoTState(gsnr_db=18.0, osnr_db=20.0, margin_db=-0.5))
    res = ip_routing.simulate_ip_routing(n)
    util = {u.ip_link_id: u for u in res.utilizations}
    assert util["ipBC"].down is True
    assert util["ipBC"].utilization is None
    assert res.down_links == ("ipBC",)
    assert res.dropped_services == (
        ip_routing.DroppedService(service_id="svc-AC", reason="link_down",
                                  on_link="ipBC"),
    )


def test_simulate_link_without_qot_is_total_not_raise():
    # S5-4: a lightpath provisioned before recompute has no recorded QoT.
    # simulate_ip_routing is a read tool and must not raise LookupError out of it;
    # the link reports a distinct "unknown" state (capacity None), never down.
    from multilayer_optical_mcp.model import ip_routing
    n = _two_link_model()
    n.add_lightpath(Lightpath(id="lpX", oms_sequence=("omsAB",),
                              mode_id="200G-16QAM", center_freq_hz=193.6e12))
    n.add_ip_link(IPLink(id="ipX", a_router="R-A", z_router="R-B",
                         lightpath_id="lpX"))  # no set_qot_state -> unknown
    # A service riding the unknown link must not be dropped (unknown != down).
    n.add_service(Service(id="svc-X", src_router="R-A", dst_router="R-B",
                          demand_gbps=50.0, working_path=("ipX",)))
    res = ip_routing.simulate_ip_routing(n)  # must not raise
    util = {u.ip_link_id: u for u in res.utilizations}
    assert util["ipX"].capacity_gbps is None
    assert util["ipX"].utilization is None
    assert util["ipX"].down is False
    assert "ipX" not in res.down_links
    assert res.dropped_services == ()


def test_down_links_includes_idle_down_link():
    # S5-5: a down link carrying no traffic is still an outage. down_links must
    # enumerate every down link, not only the loaded ones.
    from multilayer_optical_mcp.model import ip_routing
    n = _two_link_model()  # no services -> both links idle
    n.set_qot_state("lpBC", QoTState(gsnr_db=18.0, osnr_db=20.0, margin_db=-0.5))
    res = ip_routing.simulate_ip_routing(n)
    util = {u.ip_link_id: u for u in res.utilizations}
    assert util["ipBC"].down is True
    assert util["ipBC"].offered_gbps == 0.0
    assert "ipBC" in res.down_links      # idle-but-down now enumerated
    assert "ipAB" not in res.down_links  # healthy link stays out


def test_reroute_repins_working_path():
    from multilayer_optical_mcp.model import ip_routing
    n = _two_link_model()
    # Add a direct A->C express link so a reroute target exists.
    n.add_lightpath(Lightpath(id="lpAC", oms_sequence=("omsAB", "omsBC"),
                              mode_id="200G-16QAM", center_freq_hz=193.5e12))
    n.set_qot_state("lpAC", QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=1.0))
    n.add_ip_link(IPLink(id="ipAC", a_router="R-A", z_router="R-C",
                         lightpath_id="lpAC"))
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    n.set_service_working_path("svc-AC", ("ipAC",))
    assert n.get_service("svc-AC").working_path == ("ipAC",)
    # Load now lands on ipAC, not the old two-hop path.
    load = ip_routing.offered_load_per_link(n)
    assert load["ipAC"] == 120.0
    assert load["ipAB"] == 0.0


def test_reroute_rejects_noncontiguous_path():
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    # ipAB alone goes A->B, not A->C.
    with pytest.raises(ValueError, match="does not connect"):
        n.set_service_working_path("svc-AC", ("ipAB",))


def test_reroute_rejects_unknown_link():
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    with pytest.raises(ValueError, match="unknown IP link"):
        n.set_service_working_path("svc-AC", ("ipNope",))


def test_affected_services_by_lightpath_oms_and_fiber():
    from multilayer_optical_mcp.model import ip_routing
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    n.add_service(Service(id="svc-AB", src_router="R-A", dst_router="R-B",
                          demand_gbps=40.0, working_path=("ipAB",)))
    # By lightpath id.
    assert ip_routing.affected_services(n, "lpAB") == ("svc-AB", "svc-AC")
    assert ip_routing.affected_services(n, "lpBC") == ("svc-AC",)
    # By OMS id and by fiber uid (deeper layers) resolve through the lightpath.
    assert ip_routing.affected_services(n, "omsBC") == ("svc-AC",)
    assert ip_routing.affected_services(n, "fBC") == ("svc-AC",)
    # By IP link id.
    assert ip_routing.affected_services(n, "ipAB") == ("svc-AB", "svc-AC")
    # Unknown asset -> empty, not an error.
    assert ip_routing.affected_services(n, "ghost") == ()


def test_affected_services_includes_protection_path():
    from multilayer_optical_mcp.model import ip_routing
    n = _two_link_model()
    n.add_lightpath(Lightpath(id="lpAC", oms_sequence=("omsAB", "omsBC"),
                              mode_id="200G-16QAM", center_freq_hz=193.5e12))
    n.set_qot_state("lpAC", QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=1.0))
    n.add_ip_link(IPLink(id="ipAC", a_router="R-A", z_router="R-C",
                         lightpath_id="lpAC"))
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAC",),
                          protection_path=("ipAB", "ipBC")))
    # lpBC only on the protection path -> still affected.
    assert ip_routing.affected_services(n, "lpBC") == ("svc-AC",)


# --- Phase 7 Task 2: removal + failover-aware routing + 1:1 reservation ---
from multilayer_optical_mcp.model.assets import (
    Amplifier as _Amp, Fiber as _Fiber, OMS as _OMS, Lightpath as _LP,
    IPLink as _IPLink, Service as _Svc, FiberType as _FT, TransceiverMode as _TM,
    ROADM,
)
from multilayer_optical_mcp.model.qot import QoTState as _QoT
from multilayer_optical_mcp.model.modes import ModeRegistry as _MR
from multilayer_optical_mcp.model.network import NetworkModel as _NM
from multilayer_optical_mcp.model.ip_routing import simulate_ip_routing as _sim


def _model_with_service():
    m = _NM(modes=_MR([_TM(
        id="400G@7.1dB", bitrate_gbps=400.0, required_gsnr_db=7.1,
        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)]))
    m.register_fiber_type(_FT(type_variety="SSMF", loss_coef_db_per_km=0.2))
    m.add_amplifier(_Amp(id="a1", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(_Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    for node in ("A", "B"):
        m.add_roadm(ROADM(id=f"roadm_{node}"))
    m.add_oms(_OMS(id="omsAB", src_node_id="A", dst_node_id="B", elements=("roadm_A", "a1", "fAB")))
    m.add_lightpath(_LP(id="lpAB", oms_sequence=("omsAB",),
                        mode_id="400G@7.1dB", center_freq_hz=193.4e12))
    m.add_router(Router(id="rA", site="A"))
    m.add_router(Router(id="rB", site="B"))
    m.add_ip_link(_IPLink(id="ipAB", a_router="rA", z_router="rB", lightpath_id="lpAB"))
    m.add_service(_Svc(id="svc", src_router="rA", dst_router="rB",
                       demand_gbps=100.0, working_path=("ipAB",)))
    m.set_qot_state("lpAB", _QoT(gsnr_db=10.0, osnr_db=22.0, margin_db=2.0))
    return m


def test_remove_ip_link_then_service_is_dropped():
    m = _model_with_service()
    m.remove_ip_link("ipAB")
    res = _sim(m)        # must not KeyError on the missing link
    assert "svc" in {d.service_id for d in res.dropped_services}
    assert any(d.reason == "link_removed" for d in res.dropped_services)


def test_remove_lightpath_also_unbinds_ip_link():
    m = _model_with_service()
    m.remove_lightpath("lpAB")          # removes the lightpath and its bound IP link
    assert "lpAB" not in m._lightpaths
    assert "ipAB" not in m._ip_links
    res = _sim(m)
    assert "svc" in {d.service_id for d in res.dropped_services}


def _protected_model():
    from dataclasses import replace
    m = _model_with_service()                       # svc demand 100, working ("ipAB",)
    m.add_amplifier(_Amp(id="a3", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(_Fiber(id="fCD", a_end="a3", z_end="a4", length_km=80.0, type_variety="SSMF"))
    m.add_oms(_OMS(id="omsCD", src_node_id="A", dst_node_id="B", elements=("roadm_A", "a3", "fCD")))
    m.add_lightpath(_LP(id="lpCD", oms_sequence=("omsCD",),
                        mode_id="400G@7.1dB", center_freq_hz=193.5e12))
    m.add_ip_link(_IPLink(id="ipCD", a_router="rA", z_router="rB", lightpath_id="lpCD"))
    m.set_qot_state("lpCD", _QoT(gsnr_db=10.0, osnr_db=22.0, margin_db=2.0))
    m._services["svc"] = replace(m.get_service("svc"), protection_path=("ipCD",))
    return m


def test_working_failure_fails_over_to_protection_not_dropped():
    m = _protected_model()
    m.set_qot_state("lpAB", _QoT(gsnr_db=5.0, osnr_db=18.0, margin_db=-1.0))  # working dark
    res = _sim(m)
    assert "svc" not in {d.service_id for d in res.dropped_services}   # survived
    assert "svc" in res.restored_services                             # via protection
    util = {u.ip_link_id: u for u in res.utilizations}
    assert util["ipCD"].offered_gbps == 100.0                        # load moved to protection
    assert util["ipAB"].offered_gbps == 0.0                          # off the dead working link


def test_both_paths_down_drops_service():
    m = _protected_model()
    m.set_qot_state("lpAB", _QoT(gsnr_db=5.0, osnr_db=18.0, margin_db=-1.0))
    m.set_qot_state("lpCD", _QoT(gsnr_db=5.0, osnr_db=18.0, margin_db=-1.0))
    res = _sim(m)
    assert "svc" in {d.service_id for d in res.dropped_services}
    assert "svc" not in res.restored_services


def test_reserved_capacity_sums_protection_demand():
    from multilayer_optical_mcp.model.ip_routing import reserved_capacity_per_link
    m = _protected_model()                          # svc reserves 100 on ipCD
    reserved = reserved_capacity_per_link(m)
    assert reserved["ipCD"] == 100.0
    assert reserved["ipAB"] == 0.0                  # working link carries no reservation


def _disjoint_two_route_topology():
    """A->Z over two disjoint OMS (north/south), routers at A and Z, for a real
    solve_allocation protected placement. Mirrors test_allocation.py's _two_routes()."""
    reg = _MR([
        _TM(id="100G-QPSK", bitrate_gbps=100.0, required_gsnr_db=5.0,
            symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    n = _NM(modes=reg)
    n.register_fiber_type(_FT(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for a in ("aN1", "aN2", "aS1", "aS2"):
        n.add_amplifier(_Amp(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(_Fiber(id="fN", a_end="aN1", z_end="aN2", length_km=80.0, type_variety="SSMF"))
    n.add_fiber(_Fiber(id="fS", a_end="aS1", z_end="aS2", length_km=120.0, type_variety="SSMF"))
    for node in ("A", "Z"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(_OMS(id="oms-north", src_node_id="A", dst_node_id="Z",
                  elements=("roadm_A", "aN1", "fN", "aN2")))
    n.add_oms(_OMS(id="oms-south", src_node_id="A", dst_node_id="Z",
                  elements=("roadm_A", "aS1", "fS", "aS2")))
    n.add_router(Router(id="r_A", site="A"))
    n.add_router(Router(id="r_Z", site="Z"))
    return n


def test_solve_allocation_protected_service_fails_over_to_real_protection():
    from multilayer_optical_mcp.model.allocation import solve_allocation_model
    from multilayer_optical_mcp.model.solvers import SolverStatus

    class _HiQot:
        def __call__(self, *, oms_sequence, direction, mode_id, loading):
            return _QoT(gsnr_db=16.0, osnr_db=18.0, margin_db=4.0)

    n = _disjoint_two_route_topology()
    res, work = solve_allocation_model(n, _HiQot(),
                           [{"id": "d1", "src": "A", "dst": "Z",
                             "demand_gbps": 100.0, "protected": True}],
                           spare_inventory={"A": 2, "Z": 2})
    assert res.status is SolverStatus.SOLUTION
    svc = work.get_service("d1")
    assert svc.working_path and svc.protection_path      # both legs really populated

    # Fail the working leg by driving its lightpath's margin negative -- same
    # technique test_working_failure_fails_over_to_protection_not_dropped uses on a
    # hand-built fixture; here the fixture is a REAL solve_allocation output.
    working_lp_id = work.get_ip_link(svc.working_path[0]).lightpath_id
    work.set_qot_state(working_lp_id, _QoT(gsnr_db=2.0, osnr_db=5.0, margin_db=-1.0))

    result = _sim(work)
    assert "d1" not in {d.service_id for d in result.dropped_services}
    assert "d1" in result.restored_services
    prot_link = svc.protection_path[0]
    util = {u.ip_link_id: u for u in result.utilizations}
    assert util[prot_link].offered_gbps == 100.0
