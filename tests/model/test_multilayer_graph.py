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


# ---------------------------------------------------------------------------
# place_demands tests
# ---------------------------------------------------------------------------

from multilayer_optical_mcp.model.assets import (
    FiberType as _FT, Fiber as _F, Amplifier as _A, OMS as _O, Lightpath as _L,
    Router as _R, IPLink as _I, Service,
)
from multilayer_optical_mcp.model.multilayer_graph import place_demands
from multilayer_optical_mcp.model.qot import QoTState


class FakeQot:
    def __init__(self, gsnr): self._g = gsnr
    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        return QoTState(gsnr_db=self._g, osnr_db=30.0, margin_db=0.0)


def test_groom_only_reuses_existing_lightpath():
    n = _one_lightpath_model()
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="A", dst="B",
                        demand_gbps=40.0, policy="groom_only")
    assert res, "expected at least one placement"
    assert res[0].reused_lightpaths == ("lp-AB",)
    assert res[0].new_lightpaths == ()
    assert res[0].restored_gbps == 40.0          # fits in 100G residual
    assert res[0].shortfall_gbps == 0.0


def test_groom_only_degrades_to_bottleneck_residual():
    n = _one_lightpath_model()
    # load 70G onto the IP link via a background service so residual is 30G < demand
    n.add_service(Service("s-load", "R1", "R2", 70.0, working_path=("ip-AB",)))
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="A", dst="B",
                        demand_gbps=40.0, policy="groom_only")
    assert res[0].restored_gbps == 30.0
    assert res[0].shortfall_gbps == 10.0


def test_new_only_lights_new_lightpath_when_no_existing_path():
    n = _one_lightpath_model()
    # Demand B->A: no existing lightpath that direction, must light a new one.
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="B", dst="A",
                        demand_gbps=100.0, policy="new_only")
    assert res
    assert res[0].reused_lightpaths == ()
    assert len(res[0].new_lightpaths) == 1
    assert res[0].new_lightpaths[0].oms_sequence == ("oms-AB",)
    assert res[0].restored_gbps == 100.0


def test_new_run_records_travel_direction_not_physical_oms_order():
    """A B->A run realized over the physically-A->B oms-AB must record the actual
    travel endpoints (B->A), so provisioning does not derive a reversed lightpath
    from oms_sequence's physical-OMS order."""
    n = _one_lightpath_model()
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="B", dst="A",
                        demand_gbps=100.0, policy="new_only")
    run = res[0].new_lightpaths[0]
    assert run.oms_sequence == ("oms-AB",)     # physical-OMS order (unchanged)
    assert run.src_node == "B"                 # travel direction
    assert run.dst_node == "A"


def test_groom_only_empty_when_no_existing_path():
    n = _one_lightpath_model()
    g = build_layered_graph(n)
    assert place_demands(n, g, FakeQot(15.0), src="B", dst="A",
                         demand_gbps=10.0, policy="groom_only") == []


def _groom_plus_gap_model() -> NetworkModel:
    """A->M has an existing lightpath (lp-AM); M->B has free spectrum but NO
    existing lightpath. Demand A->B must groom A->M then light a new M->B
    lightpath -> a hybrid placement."""
    n = NetworkModel(modes=ModeRegistry([
        _TM_helper()]))
    n.register_fiber_type(_FT("SSMF", 0.2))
    for a in ("m1", "m2", "n1", "n2"):
        n.add_amplifier(_A(a, "advanced_toy", 20.0, 5.5))
    n.add_fiber(_F("fAM", "m1", "m2", 60.0, "SSMF"))
    n.add_fiber(_F("fMB", "n1", "n2", 60.0, "SSMF"))
    n.add_oms(_O("oms-AM", "A", "M", ("m1", "fAM", "m2")))
    n.add_oms(_O("oms-MB", "M", "B", ("n1", "fMB", "n2")))
    n.add_lightpath(_L("lp-AM", ("oms-AM",), "100G", 193.4e12))
    n.set_qot_state("lp-AM", QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=3.0))
    n.add_router(_R("RA", "A"))
    n.add_router(_R("RM", "M"))
    n.add_ip_link(_I("ip-AM", "RA", "RM", "lp-AM"))
    return n


def _TM_helper():
    from multilayer_optical_mcp.model.assets import TransceiverMode
    return TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                           symbol_rate_baud=32e9, channel_spacing_hz=100e9)


def test_groom_or_new_finds_hybrid_groom_plus_new():
    n = _groom_plus_gap_model()
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="A", dst="B",
                        demand_gbps=100.0, policy="groom_or_new")
    hybrids = [p for p in res if p.reused_lightpaths and p.new_lightpaths]
    assert hybrids, "expected a hybrid (groom A->M + new M->B)"
    h = hybrids[0]
    assert h.reused_lightpaths == ("lp-AM",)
    assert h.new_lightpaths[0].oms_sequence == ("oms-MB",)
    assert h.restored_gbps == 100.0
