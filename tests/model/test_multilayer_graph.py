# tests/model/test_multilayer_graph.py
"""Layered auxiliary graph: existing lightpaths -> LPE edges (residual,
margin-gated); free wavelengths -> WLE edges driven from the OMS bitmask."""
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, Router, IPLink, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.spectrum import SpectrumGrid
from multilayer_optical_mcp.model.multilayer_graph import (
    build_layered_graph, ACCESS, WL, lpe_edges, wle_count_on_layer,
)


def _one_lightpath_model(margin_db: float = 3.0) -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=100e9)]))
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("a1", "a2"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber("fAB", "a1", "a2", 80.0, "SSMF"))
    n.add_oms(OMS("oms-AB", "A", "B", ("a1", "fAB", "a2")))
    # Lightpath on slot 20 (193.4 THz on the default grid).
    n.add_lightpath(Lightpath("lp-AB", ("oms-AB",), "100G", 193.4e12))
    n.set_qot_state("lp-AB", QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=margin_db))
    n.add_router(Router("R1", "A"))
    n.add_router(Router("R2", "B"))
    n.add_ip_link(IPLink("ip-AB", "R1", "R2", "lp-AB"))
    return n


def test_existing_lightpath_becomes_lpe_with_residual():
    n = _one_lightpath_model()
    g = build_layered_graph(n)
    edges = lpe_edges(g)
    assert len(edges) == 1
    (u, v, data), = edges
    assert u == (ACCESS, "A") and v == (ACCESS, "B")
    assert data["lightpath_id"] == "lp-AB"
    assert data["residual_gbps"] == 100.0     # 100G mode, no load


def test_margin_negative_lightpath_has_no_lpe_edge():
    n = _one_lightpath_model(margin_db=-1.0)   # down -> capacity 0
    g = build_layered_graph(n)
    assert lpe_edges(g) == []


def test_wle_present_only_on_free_slots():
    n = _one_lightpath_model()
    grid = SpectrumGrid.default()
    g = build_layered_graph(n)
    # slot 20 is occupied by lp-AB on oms-AB -> no WLE on layer 20
    assert wle_count_on_layer(g, "oms-AB", 20) == 0
    # slot 0 is free -> a WLE exists on layer 0 for oms-AB (both directions)
    assert wle_count_on_layer(g, "oms-AB", 0) == 2


def test_forbidden_asset_drops_lpe_and_wle():
    n = _one_lightpath_model()
    g = build_layered_graph(n, forbidden_assets=frozenset({"fAB"}))
    assert lpe_edges(g) == []                  # lightpath crosses fAB
    assert wle_count_on_layer(g, "oms-AB", 0) == 0   # OMS pruned entirely
