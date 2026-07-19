# tests/model/test_multilayer_graph.py
"""Layered auxiliary graph: existing lightpaths -> LPE edges (residual,
margin-gated); free wavelengths -> WLE edges driven from the OMS bitmask."""
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, Router, IPLink, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.spectrum import SpectrumGrid
from multilayer_optical_mcp.model.multilayer_graph import (
    build_layered_graph, ACCESS, WL, lpe_edges, wle_count_on_layer, place_demands,
)
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph


class _ConstQot:
    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        return QoTState(gsnr_db=16.0, osnr_db=30.0, margin_db=0.0)


def _line3() -> NetworkModel:
    """Importer-built 0-1-2 line: both directed OMS per fiber, so a 0->2 demand
    routes 0->1->2 and its new lightpath must chain oms_0_1 then oms_1_2."""
    graph = {
        "nodes": [{"id": i} for i in range(3)],
        "edges": [
            {"src": 0, "dst": 1, "length_km": 80.0},
            {"src": 1, "dst": 2, "length_km": 80.0},
        ],
    }
    return model_from_abstract_graph(graph, modes=ModeRegistry([
        TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=7.1,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)]))


def test_new_runs_are_direction_contiguous_through_intermediate_node():
    """Regression: place_demands must not emit a new lightpath whose oms_sequence
    walks OMS against their travel direction (multilayer_graph:174 added each OMS
    in BOTH directions, so a 0->2 route could pick oms_1_0 for the 0->1 hop)."""
    m = _line3()
    g = build_layered_graph(m)
    cands = place_demands(m, g, _ConstQot(), src="0", dst="2",
                          demand_gbps=100.0, policy="new_only", k=8)
    assert cands
    for p in cands:
        for run in p.new_lightpaths:
            seq = [m.get_oms(o) for o in run.oms_sequence]
            assert seq[0].src_node_id == run.src_node
            assert seq[-1].dst_node_id == run.dst_node
            for x, y in zip(seq, seq[1:]):
                assert x.dst_node_id == y.src_node_id, run.oms_sequence


def _one_lightpath_model(margin_db: float = 3.0) -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=100e9)]))
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("a1", "a2"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber("fAB", "a1", "a2", 80.0, "SSMF"))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS("oms-AB", "A", "B", ("roadm_A", "a1", "fAB", "a2")))
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
    # slot 0 is free -> one WLE on layer 0 for oms-AB (its A->B direction only)
    assert wle_count_on_layer(g, "oms-AB", 0) == 1


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


def test_new_only_lights_new_lightpath_ignoring_existing():
    n = _one_lightpath_model()
    # new_only drops LPE (grooming) edges, so the A->B demand cannot reuse lp-AB
    # and must light a FRESH A->B lightpath on a free slot.
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="A", dst="B",
                        demand_gbps=100.0, policy="new_only")
    assert res
    assert res[0].reused_lightpaths == ()
    assert len(res[0].new_lightpaths) == 1
    assert res[0].new_lightpaths[0].oms_sequence == ("oms-AB",)
    assert res[0].restored_gbps == 100.0


def _symmetric_two_node() -> NetworkModel:
    """A<->B with BOTH directed OMS, as a real importer builds — so a B->A demand
    routes over the B->A OMS rather than traversing an A->B OMS backwards."""
    n = NetworkModel(modes=ModeRegistry([_TM_helper()]))
    n.register_fiber_type(_FT("SSMF", 0.2))
    for a in ("s1", "s2", "s3", "s4"):
        n.add_amplifier(_A(a, "advanced_toy", 20.0, 5.5))
    n.add_fiber(_F("fAB", "s1", "s2", 80.0, "SSMF"))
    n.add_fiber(_F("fBA", "s3", "s4", 80.0, "SSMF"))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(_O("oms-AB", "A", "B", ("roadm_A", "s1", "fAB", "s2")))
    n.add_oms(_O("oms-BA", "B", "A", ("roadm_B", "s3", "fBA", "s4")))
    return n


def test_new_run_records_travel_endpoints():
    """A new run records its travel endpoints (src_node/dst_node), which drive
    provisioning. A B->A demand lights a lightpath over the B->A OMS."""
    n = _symmetric_two_node()
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="B", dst="A",
                        demand_gbps=100.0, policy="new_only")
    run = res[0].new_lightpaths[0]
    assert run.oms_sequence == ("oms-BA",)     # travels the B->A OMS
    assert run.src_node == "B"
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


def _cheap_route_plus_distinct_route() -> NetworkModel:
    """A cheap 1-hop A->B route and a distinct 2-hop A->C->B route. Driven with a
    wide grid, the cheap route's lambda-variants exceed the raw-path budget, so a
    naive (per-emission) budget never reaches the strictly-more-expensive 2-hop
    route."""
    n = NetworkModel(modes=ModeRegistry([_TM_helper()]))
    n.register_fiber_type(_FT("SSMF", 0.2))
    for a in ("aba", "abz", "aca", "acz", "cba", "cbz"):
        n.add_amplifier(_A(a, "advanced_toy", 20.0, 5.5))
    n.add_fiber(_F("fab", "aba", "abz", 80.0, "SSMF"))
    n.add_fiber(_F("fac", "aca", "acz", 80.0, "SSMF"))
    n.add_fiber(_F("fcb", "cba", "cbz", 80.0, "SSMF"))
    n.add_oms(_O("oms-AB", "A", "B", ("aba", "fab", "abz")))
    n.add_oms(_O("oms-AC", "A", "C", ("aca", "fac", "acz")))
    n.add_oms(_O("oms-CB", "C", "B", ("cba", "fcb", "cbz")))
    return n


def test_new_only_budget_not_starved_by_wavelength_variants():
    """The distinct 2-hop route must be reachable even though >_PATH_BUDGET
    lambda-variants of the cheaper 1-hop route precede it in weight order (they
    would exhaust a raw-per-emission budget before the distinct route is seen)."""
    from multilayer_optical_mcp.model.spectrum import SpectrumGrid
    grid = SpectrumGrid(anchor_hz=191.4e12, spacing_hz=100e9, num_slots=80)
    n = _cheap_route_plus_distinct_route()
    g = build_layered_graph(n, grid=grid)
    res = place_demands(n, g, FakeQot(15.0), src="A", dst="B",
                        demand_gbps=100.0, policy="new_only", grid=grid)
    routes = {p.new_lightpaths[0].oms_sequence for p in res if p.new_lightpaths}
    assert ("oms-AC", "oms-CB") in routes, routes


def _two_parallel_oms_model() -> NetworkModel:
    """Two parallel A->B OMS (oms-AB-1, oms-AB-2) on physically-separate fibers,
    no existing lightpath. A new A->B lightpath can be lit on EITHER fiber, so
    both routes must be enumerable. On a plain nx.DiGraph the two WLE edges share
    the ordered vertex pair ((WL,A,lam)->(WL,B,lam)) per slot and the second
    overwrites the first (S7-13), collapsing them to one route."""
    n = NetworkModel(modes=ModeRegistry([_TM_helper()]))
    n.register_fiber_type(_FT("SSMF", 0.2))
    for a in ("p1a", "p1z", "p2a", "p2z"):
        n.add_amplifier(_A(a, "advanced_toy", 20.0, 5.5))
    n.add_fiber(_F("fab1", "p1a", "p1z", 80.0, "SSMF"))
    n.add_fiber(_F("fab2", "p2a", "p2z", 80.0, "SSMF"))
    n.add_oms(_O("oms-AB-1", "A", "B", ("p1a", "fab1", "p1z")))
    n.add_oms(_O("oms-AB-2", "A", "B", ("p2a", "fab2", "p2z")))
    return n


def test_parallel_oms_both_routes_enumerable():
    """S7-13: both parallel A->B fibers must surface as distinct new-lightpath
    routes; the DiGraph WL-layer overwrite silently kept only the last-added one."""
    n = _two_parallel_oms_model()
    g = build_layered_graph(n)
    res = place_demands(n, g, FakeQot(15.0), src="A", dst="B",
                        demand_gbps=100.0, policy="new_only")
    routes = {p.new_lightpaths[0].oms_sequence for p in res if p.new_lightpaths}
    assert routes == {("oms-AB-1",), ("oms-AB-2",)}, routes


def test_wle_count_counts_parallel_oms_per_layer():
    """Each parallel OMS contributes its own WLE edge per free slot (one, in its
    A->B direction); the DiGraph overwrite would collapse them to a single edge."""
    n = _two_parallel_oms_model()
    g = build_layered_graph(n)
    assert wle_count_on_layer(g, "oms-AB-1", 0) == 1
    assert wle_count_on_layer(g, "oms-AB-2", 0) == 1


# ---------------------------------------------------------------------------
# O2: hoist the offered-load map + min-over-bound-links residual
# ---------------------------------------------------------------------------

def _multi_link_lightpath_model() -> NetworkModel:
    """One lightpath (lp-AB) bound to TWO IP links (ip-AB loaded 60G, ip-AB2
    loaded 20G), both reading the 100G mode. residual_gbps per link:
    ip-AB -> 40, ip-AB2 -> 80. `max` reports 80 (the healthy link); `min`
    reports 40 (the bottleneck link a groom is actually limited by)."""
    n = _one_lightpath_model()               # lp-AB, ip-AB (R1->R2)
    n.add_router(_R("R3", "A"))
    n.add_router(_R("R4", "B"))
    n.add_ip_link(_I("ip-AB2", "R3", "R4", "lp-AB"))  # 2nd link on same lightpath
    n.add_service(Service("s1", "R1", "R2", 60.0, working_path=("ip-AB",)))
    n.add_service(Service("s2", "R3", "R4", 20.0, working_path=("ip-AB2",)))
    return n


def test_residual_is_min_over_bound_ip_links_not_max():
    """A lightpath serving two IP links, one more loaded than the other, reports
    the BOTTLENECK residual (min), not the healthiest link's headroom (max) —
    max would overstate capacity the groom can't actually use."""
    n = _multi_link_lightpath_model()
    g = build_layered_graph(n)
    (u, v, data), = lpe_edges(g)
    assert data["residual_gbps"] == 40.0     # min(40, 80), not max


def _two_ip_bound_lightpaths_model() -> NetworkModel:
    """Two lightpaths, each bound to its own IP link, so both exercise the
    load-map branch of _residual_gbps (the no-IP-link branch skips the map)."""
    n = _one_lightpath_model()               # oms-AB, lp-AB, ip-AB
    for a in ("c1", "c2"):
        n.add_amplifier(_A(a, "advanced_toy", 20.0, 5.5))
    n.add_fiber(_F("fCD", "c1", "c2", 80.0, "SSMF"))
    n.add_oms(_O("oms-CD", "C", "D", ("c1", "fCD", "c2")))
    n.add_lightpath(_L("lp-CD", ("oms-CD",), "100G", 193.4e12))
    n.set_qot_state("lp-CD", QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=3.0))
    n.add_router(_R("R3", "C"))
    n.add_router(_R("R4", "D"))
    n.add_ip_link(_I("ip-CD", "R3", "R4", "lp-CD"))
    return n


def test_offered_load_map_built_once_per_graph_build(monkeypatch):
    """S5-8/S7-8: the offered-load map is built ONCE per build_layered_graph,
    not rebuilt inside the per-lightpath loop (O(L·S) -> O(L+S))."""
    from multilayer_optical_mcp.model import ip_routing
    n = _two_ip_bound_lightpaths_model()
    calls = {"n": 0}
    real = ip_routing.offered_load_per_link

    def counting(model):
        calls["n"] += 1
        return real(model)

    monkeypatch.setattr(ip_routing, "offered_load_per_link", counting)
    build_layered_graph(n)
    assert calls["n"] == 1                   # once, despite two lightpaths


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
