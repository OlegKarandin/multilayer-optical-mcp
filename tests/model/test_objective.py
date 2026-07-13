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
    assert r.transponders == 2.0 * len(loaded_model.list_lightpaths())
    assert r.max_util >= 0.0
    # default weights = 1.0, total_margin subtracted:
    assert r.scalar == (r.spectrum_used + r.transponders + r.max_util
                        + r.dropped_traffic + r.added_latency
                        + r.services_at_risk - r.total_margin)


def test_margin_negative_lightpath_scores_as_dropped_not_nominal(down_model):
    # a lightpath seeded margin<0 -> its IP link capacity 0 -> its service dropped
    r = evaluate_objective(down_model)
    assert r.dropped_traffic > 0.0
