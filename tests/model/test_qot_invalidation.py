"""S1-7: physics mutations must invalidate stale QoT so ip_link_capacity_gbps
never silently serves a capacity read off a mode/impairment that changed."""
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, TransceiverMode, Router, IPLink,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState


def _model() -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
        TransceiverMode(id="200G-16QAM", bitrate_gbps=200.0,
                        required_gsnr_db=18.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                  elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id="200G-16QAM", center_freq_hz=193.4e12))
    n.add_router(Router(id="R-A", site="A"))
    n.add_router(Router(id="R-B", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R-A", z_router="R-B",
                         lightpath_id="lp1"))
    n.set_qot_state("lp1", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=4.0))
    return n


def test_set_lightpath_mode_clears_that_lightpaths_qot():
    n = _model()
    assert n.ip_link_capacity_gbps("ip1") == 200.0  # baseline
    n.set_lightpath_mode("lp1", "100G-QPSK")
    with pytest.raises(LookupError):
        n.get_qot_state("lp1")
    with pytest.raises(LookupError):
        n.ip_link_capacity_gbps("ip1")


def test_apply_nf_delta_clears_all_qot():
    n = _model()
    n.apply_nf_delta("amp1", 3.0)
    with pytest.raises(LookupError):
        n.get_qot_state("lp1")


def test_apply_loss_delta_clears_all_qot():
    n = _model()
    n.apply_loss_delta("f1", 2.0)
    with pytest.raises(LookupError):
        n.get_qot_state("lp1")
