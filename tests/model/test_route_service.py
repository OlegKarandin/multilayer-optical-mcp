# tests/model/test_route_service.py
"""route_service: unified service-level routing/restoration menu (unprotected)
and disjoint-pair menu (protected). Read-only; scores via objective.score_candidate
/ score_pair on throwaway clones; ground truth untouched."""
import pytest

from multilayer_optical_mcp.model.assets import FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, TransceiverMode
from multilayer_optical_mcp.model.ip_assets import Router, IPLink, Service
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.multilayer_graph import Placement, NewLightpathRun
from multilayer_optical_mcp.model.objective import placement_materializable
from multilayer_optical_mcp.model.route_service import route_service


class FakeQot:
    def __init__(self, gsnr=15.0): self._g = gsnr
    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        return QoTState(gsnr_db=self._g, osnr_db=30.0, margin_db=0.0)


def _empty_net_model() -> NetworkModel:
    """Empty net: single OMS A->B, two routers, one service, no lightpaths yet
    -> first-time routing must synthesize an all-new candidate."""
    reg = ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=10.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="a1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0,
                      type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="a2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "a1", "fAB", "a2")))
    n.add_router(Router(id="RA", site="A"))
    n.add_router(Router(id="RB", site="B"))
    n.add_service(Service(id="svc-AB", src_router="RA", dst_router="RB",
                          demand_gbps=100.0, working_path=()))
    return n


@pytest.fixture
def diamond_service():
    n = _empty_net_model()
    return n, n.get_service("svc-AB")


def _two_route_model() -> NetworkModel:
    """Empty net, two vertex/fiber-disjoint detours A-M1-B and A-M2-B, no
    existing lightpaths -> first-time protected routing must find a fully
    disjoint pair of new-lightpath candidates."""
    reg = ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=10.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for a in ("aAM1a", "aAM1b", "aM1Ba", "aM1Bb",
              "aAM2a", "aAM2b", "aM2Ba", "aM2Bb"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAM1", a_end="aAM1a", z_end="aAM1b", length_km=60.0,
                      type_variety="SSMF"))
    n.add_fiber(Fiber(id="fM1B", a_end="aM1Ba", z_end="aM1Bb", length_km=60.0,
                      type_variety="SSMF"))
    n.add_fiber(Fiber(id="fAM2", a_end="aAM2a", z_end="aAM2b", length_km=60.0,
                      type_variety="SSMF"))
    n.add_fiber(Fiber(id="fM2B", a_end="aM2Ba", z_end="aM2Bb", length_km=60.0,
                      type_variety="SSMF"))
    for node in ("A", "B", "M1", "M2"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="omsAM1", src_node_id="A", dst_node_id="M1",
                  elements=("roadm_A", "aAM1a", "fAM1", "aAM1b")))
    n.add_oms(OMS(id="omsM1B", src_node_id="M1", dst_node_id="B",
                  elements=("roadm_M1", "aM1Ba", "fM1B", "aM1Bb")))
    n.add_oms(OMS(id="omsAM2", src_node_id="A", dst_node_id="M2",
                  elements=("roadm_A", "aAM2a", "fAM2", "aAM2b")))
    n.add_oms(OMS(id="omsM2B", src_node_id="M2", dst_node_id="B",
                  elements=("roadm_M2", "aM2Ba", "fM2B", "aM2Bb")))
    n.add_router(Router(id="RA", site="A"))
    n.add_router(Router(id="RB", site="B"))
    # Routers at the intermediate ROADM sites too: place_demands can realize a
    # route as two back-to-back new-lightpath runs meeting at a waypoint (an
    # "IP-hop-in-the-middle" split, e.g. a fresh run A->M2 plus a fresh run
    # M2->B), and apply_candidate/score_pair provisions each run's endpoints as
    # IP links -> every OMS-endpoint node needs a router, same convention as
    # test_restoration.py's _diamond fixture (Router at every node, incl. M).
    n.add_router(Router(id="RM1", site="M1"))
    n.add_router(Router(id="RM2", site="M2"))
    n.add_service(Service(id="svc", src_router="RA", dst_router="RB",
                          demand_gbps=100.0, working_path=()))
    return n


@pytest.fixture
def diamond_service_two_routes():
    n = _two_route_model()
    return n, n.get_service("svc")


def _shrunk_model() -> NetworkModel:
    """Single OMS A->B already lit by lpAB (unrouted service) -> grooming onto
    lpAB and lighting a fresh run on the SAME omsAB (a different wavelength) are
    the only two routes, and they share the same fiber -> no fully disjoint
    pair exists; best_effort must surface the overlapping pair as PARTIAL."""
    reg = ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=10.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="a1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0,
                      type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="a2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "a1", "fAB", "a2")))
    n.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",),
                              mode_id="100G", center_freq_hz=193.4e12))
    n.set_qot_state("lpAB", QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=5.0))
    n.add_router(Router(id="RA", site="A"))
    n.add_router(Router(id="RB", site="B"))
    n.add_ip_link(IPLink(id="ipAB", a_router="RA", z_router="RB",
                         lightpath_id="lpAB"))
    n.add_service(Service(id="svc", src_router="RA", dst_router="RB",
                          demand_gbps=50.0, working_path=()))
    return n


@pytest.fixture
def diamond_service_shrunk():
    n = _shrunk_model()
    return n, n.get_service("svc")


def _routerless_waypoint_model() -> NetworkModel:
    """Empty net, linear A-M-B over two OMS, with NO router at the pass-through
    ROADM M (routers only at A and B). place_demands can realize A->B as ONE
    continuous run (endpoints A,B -> materializable) AND as a SPLIT pair of runs
    A->M then M->B (an IP hop at M -> endpoint M has no router -> NOT
    materializable). The split candidate must be excluded, not crashed on."""
    reg = ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=10.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for a in ("aAMa", "aAMb", "aMBa", "aMBb"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAM", a_end="aAMa", z_end="aAMb", length_km=60.0,
                      type_variety="SSMF"))
    n.add_fiber(Fiber(id="fMB", a_end="aMBa", z_end="aMBb", length_km=60.0,
                      type_variety="SSMF"))
    for node in ("A", "M", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="omsAM", src_node_id="A", dst_node_id="M",
                  elements=("roadm_A", "aAMa", "fAM", "aAMb")))
    n.add_oms(OMS(id="omsMB", src_node_id="M", dst_node_id="B",
                  elements=("roadm_M", "aMBa", "fMB", "aMBb")))
    # Routers only at A and B -- M is a router-less pass-through ROADM.
    n.add_router(Router(id="RA", site="A"))
    n.add_router(Router(id="RB", site="B"))
    n.add_service(Service(id="svc", src_router="RA", dst_router="RB",
                          demand_gbps=100.0, working_path=()))
    return n


@pytest.fixture
def diamond_service_routerless_waypoint():
    n = _routerless_waypoint_model()
    return n, n.get_service("svc")


def _unbound_lightpath_model() -> NetworkModel:
    """Single OMS A->B already lit by lpAB, but with NO IP link bound to it --
    a valid grooming target per _residual_gbps ('a lightpath with no IP link
    bound yields its full mode rate'). place_demands's groom_or_new policy can
    reuse lpAB directly (full mode-rate residual capacity), producing a
    Placement whose reused_lightpaths names an IP-unbound lightpath -- the case
    placement_materializable must exclude and apply_candidate would otherwise
    KeyError on (lp_to_iplink has no entry for lpAB)."""
    reg = ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=10.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="a1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0,
                      type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="a2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "a1", "fAB", "a2")))
    n.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",),
                              mode_id="100G", center_freq_hz=193.4e12))
    n.set_qot_state("lpAB", QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=5.0))
    # NOTE: deliberately no add_ip_link -- lpAB is unbound.
    n.add_router(Router(id="RA", site="A"))
    n.add_router(Router(id="RB", site="B"))
    n.add_service(Service(id="svc", src_router="RA", dst_router="RB",
                          demand_gbps=50.0, working_path=()))
    return n


@pytest.fixture
def diamond_service_unbound_lightpath():
    n = _unbound_lightpath_model()
    return n, n.get_service("svc")


def test_placement_materializable_excludes_unbound_reused_lightpath():
    unbound_model = _unbound_lightpath_model()
    reused = Placement(reused_lightpaths=("lpAB",), new_lightpaths=(),
                       restored_gbps=50.0, shortfall_gbps=0.0)
    assert placement_materializable(unbound_model, reused) is False

    bound_model = _shrunk_model()   # lpAB IS bound to ipAB here
    assert placement_materializable(bound_model, reused) is True


def test_route_service_unbound_reused_lightpath_returns_typed_not_raises(
        diamond_service_unbound_lightpath):
    # Regression: a placement that grooms onto an IP-unbound lightpath must NOT
    # raise KeyError in apply_candidate; route_service returns a typed result
    # and no surviving candidate reuses the unbound lightpath.
    model, svc = diamond_service_unbound_lightpath
    res = route_service(model, FakeQot(), svc.id)      # must not raise
    assert isinstance(res.status, SolverStatus)
    for c in res.candidates:
        assert "lpAB" not in c.reused_lightpaths


def test_placement_materializable_predicate():
    # A new run terminating at router-less node "M" -> not materializable.
    split = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAM",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="M"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    # A run whose endpoints are both router sites -> materializable.
    whole = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAM", "omsMB"), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    model = _routerless_waypoint_model()
    assert placement_materializable(model, split) is False
    assert placement_materializable(model, whole) is True


def test_routerless_waypoint_returns_typed_not_raises(diamond_service_routerless_waypoint):
    # Regression: a split placement through a router-less pass-through ROADM must
    # NOT raise KeyError; route_service returns a typed result and every surviving
    # candidate's new-run endpoints are all router sites.
    model, svc = diamond_service_routerless_waypoint
    res = route_service(model, FakeQot(), svc.id)      # must not raise
    assert isinstance(res.status, SolverStatus)
    router_sites = {r.site for r in model.list_routers()}
    for c in res.candidates:
        for run in c.new_lightpaths:
            assert run.src_node in router_sites and run.dst_node in router_sites


def test_unprotected_first_time_routing_menu(diamond_service):
    model, svc = diamond_service   # empty net
    # Lock the read-only / no-consume contract: ground truth untouched.
    lps_before = len(model.list_lightpaths())
    ipl_before = len(model.list_ip_links())
    res = route_service(model, FakeQot(), svc.id)     # avoid=None -> first-time
    assert res.status in (SolverStatus.SOLUTION, SolverStatus.PARTIAL)
    assert res.candidates                              # a menu of candidates
    assert all("cost_vector" in vars(c) or hasattr(c, "cost_vector") for c in res.candidates)
    # sorted ascending by scalar:
    assert res.candidates == tuple(sorted(res.candidates,
        key=lambda c: c.cost_vector["scalar"]))
    assert len(model.list_lightpaths()) == lps_before   # no-consume
    assert len(model.list_ip_links()) == ipl_before


def test_protected_returns_disjoint_pair_menu(diamond_service_two_routes):
    model, svc = diamond_service_two_routes
    res = route_service(model, FakeQot(), svc.id, protected=True, best_effort=False)
    assert res.protected and res.pairs and res.pairs[0].disjoint


def test_protected_best_effort_partial(diamond_service_shrunk):
    model, svc = diamond_service_shrunk
    res = route_service(model, FakeQot(), svc.id, protected=True, best_effort=True)
    assert res.status is SolverStatus.PARTIAL and not res.pairs[0].disjoint


def test_route_service_threads_one_grid_through_layered_and_placement(monkeypatch):
    """S7-12 fix: build_layered_graph and place_demands must share one
    SpectrumGrid instance per route_service call, not two independently
    defaulted ones that could desync if a non-default grid were ever used."""
    import multilayer_optical_mcp.model.multilayer_graph as _mg

    n = _empty_net_model()
    seen_grids = []
    real_build_spectrum_state = _mg.build_spectrum_state

    def _spy(model, grid):
        seen_grids.append(grid)
        return real_build_spectrum_state(model, grid)

    monkeypatch.setattr(_mg, "build_spectrum_state", _spy)
    route_service(n, FakeQot(), "svc-AB")

    assert len(seen_grids) >= 2
    assert all(g is seen_grids[0] for g in seen_grids)


def test_unprotected_scoring_planerror_is_dropped_not_raised(diamond_service, monkeypatch):
    # Regression: score_candidate materializes a placement via the real apply_op
    # path and can raise PlanError on a bad/stale reference. route_service is a
    # solver-menu tool (CLAUDE.md: typed results, never exceptions) -- before
    # the fix, nothing between here and the MCP boundary caught this, so a
    # single unscoreable candidate crashed the whole call instead of just being
    # dropped from the menu.
    import multilayer_optical_mcp.model.route_service as rs_mod
    from multilayer_optical_mcp.model.plan import PlanError

    model, svc = diamond_service

    def _boom(*args, **kwargs):
        raise PlanError("simulated bad reference during scoring")

    monkeypatch.setattr(rs_mod, "score_candidate", _boom)
    res = route_service(model, FakeQot(), svc.id)  # must not raise
    assert isinstance(res.status, SolverStatus)
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.candidates == ()


def test_protected_scoring_planerror_is_dropped_not_raised(diamond_service_two_routes, monkeypatch):
    import multilayer_optical_mcp.model.route_service as rs_mod
    from multilayer_optical_mcp.model.plan import PlanError

    model, svc = diamond_service_two_routes

    def _boom(*args, **kwargs):
        raise PlanError("simulated bad reference during scoring")

    monkeypatch.setattr(rs_mod, "score_pair", _boom)
    res = route_service(model, FakeQot(), svc.id, protected=True)  # must not raise
    assert isinstance(res.status, SolverStatus)
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.pairs == ()
