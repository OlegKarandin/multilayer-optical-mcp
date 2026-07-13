# tests/model/test_route_service.py
"""route_service: unified service-level routing/restoration menu (unprotected)
and disjoint-pair menu (protected). Read-only; scores via objective.score_candidate
/ score_pair on throwaway clones; ground truth untouched."""
import pytest

from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, Router, IPLink,
    Service, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.solvers import SolverStatus
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


def test_unprotected_first_time_routing_menu(diamond_service):
    model, svc = diamond_service   # empty net
    res = route_service(model, FakeQot(), svc.id)     # avoid=None -> first-time
    assert res.status in (SolverStatus.SOLUTION, SolverStatus.PARTIAL)
    assert res.candidates                              # a menu of candidates
    assert all("cost_vector" in vars(c) or hasattr(c, "cost_vector") for c in res.candidates)
    # sorted ascending by scalar:
    assert res.candidates == tuple(sorted(res.candidates,
        key=lambda c: c.cost_vector["scalar"]))


def test_protected_returns_disjoint_pair_menu(diamond_service_two_routes):
    model, svc = diamond_service_two_routes
    res = route_service(model, FakeQot(), svc.id, protected=True, best_effort=False)
    assert res.protected and res.pairs and res.pairs[0].disjoint


def test_protected_best_effort_partial(diamond_service_shrunk):
    model, svc = diamond_service_shrunk
    res = route_service(model, FakeQot(), svc.id, protected=True, best_effort=True)
    assert res.status is SolverStatus.PARTIAL and not res.pairs[0].disjoint
