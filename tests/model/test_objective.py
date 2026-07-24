# tests/model/test_objective.py
import pytest

from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, ROADM, Lightpath, Router, IPLink,
    Service, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.objective import evaluate_objective, ObjectiveResult


def _base_model():
    """A->B: single-OMS lightpath bound to one IP link between two routers."""
    reg = ModeRegistry([
        TransceiverMode(id="200G-16QAM", bitrate_gbps=200.0, required_gsnr_db=18.5,
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
                              mode_id="200G-16QAM", center_freq_hz=193.4e12))
    n.add_router(Router(id="R-A", site="A"))
    n.add_router(Router(id="R-B", site="B"))
    n.add_ip_link(IPLink(id="ipAB", a_router="R-A", z_router="R-B",
                         lightpath_id="lpAB"))
    n.add_service(Service(id="svc-AB", src_router="R-A", dst_router="R-B",
                          demand_gbps=80.0, working_path=("ipAB",)))
    return n


@pytest.fixture
def loaded_model():
    n = _base_model()
    n.set_qot_state("lpAB", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=3.5))
    return n


@pytest.fixture
def down_model():
    n = _base_model()
    # Margin negative -> ip_link_capacity_gbps == 0 -> svc-AB is dropped.
    n.set_qot_state("lpAB", QoTState(gsnr_db=18.0, osnr_db=20.0, margin_db=-0.5))
    return n


def test_objective_vector_on_seeded_state(loaded_model):
    r = evaluate_objective(loaded_model)
    assert isinstance(r, ObjectiveResult)
    # Ground truth for the single-OMS/single-lightpath/single-service fixture:
    # one occupied grid slot, one 80/200 Gbps IP link, nothing dropped, 80 km of
    # propagation latency on the one active lightpath, margin (3.5) above the
    # default 1.0 dB at-risk threshold.
    assert r.spectrum_used == 1
    assert r.transponders == 2.0 * len(loaded_model.list_lightpaths())
    assert r.max_util == pytest.approx(80.0 / 200.0)
    assert r.dropped_traffic == 0.0
    assert r.added_latency == pytest.approx(0.005 * 80.0)
    assert r.services_at_risk == 0
    assert r.total_margin == pytest.approx(3.5)
    # default weights = 1.0, total_margin subtracted:
    assert r.scalar == pytest.approx(
        r.spectrum_used + r.transponders + r.max_util
        + r.dropped_traffic + r.added_latency
        + r.services_at_risk - r.total_margin)


def test_margin_negative_lightpath_scores_as_dropped_not_nominal(down_model):
    # a lightpath seeded margin<0 -> its IP link capacity 0 -> its service dropped
    r = evaluate_objective(down_model)
    assert r.dropped_traffic > 0.0


def test_removed_ip_link_in_working_path_does_not_raise():
    # remove_ip_link leaves the dangling link id in the service's working_path (a
    # documented valid state; simulate_ip_routing drops the service, reason
    # "link_removed", and never raises). evaluate_objective must not KeyError on
    # the services_at_risk lookup -- it must return a typed result, with the
    # dropped service counted in dropped_traffic, not services_at_risk.
    n = _base_model()
    n.set_qot_state("lpAB", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=3.5))
    n.remove_ip_link("ipAB")   # dangling "ipAB" remains in svc-AB.working_path
    r = evaluate_objective(n)  # must not raise
    assert isinstance(r, ObjectiveResult)
    assert r.dropped_traffic >= 80.0     # svc-AB demand counted as dropped
    assert r.services_at_risk == 0       # a dropped service is not "at risk"
