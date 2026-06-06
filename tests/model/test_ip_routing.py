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
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B",
                  elements=("a1", "fAB", "a2")))
    n.add_oms(OMS(id="omsBC", src_node_id="B", dst_node_id="C",
                  elements=("a2", "fBC", "a3")))
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
