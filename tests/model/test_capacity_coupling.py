import pytest
from multilayer_optical_mcp.model.assets import FiberType, Amplifier, Fiber, OMS, ROADM, Lightpath, TransceiverMode
from multilayer_optical_mcp.model.ip_assets import Router, IPLink
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState


def _model_with_lightpath(mode_id="200G-16QAM"):
    reg = ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
        TransceiverMode(id="200G-16QAM", bitrate_gbps=200.0,
                        required_gsnr_db=18.5, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id=mode_id, center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    return n


def test_capacity_reads_mode_bitrate_when_margin_positive():
    n = _model_with_lightpath("200G-16QAM")
    n.set_qot_state("lp1", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=3.5,
                                    limiting_element_id="amp1"))
    assert n.ip_link_capacity_gbps("ip1") == 200.0


def test_capacity_follows_mode_downshift():
    n = _model_with_lightpath("200G-16QAM")
    n.set_qot_state("lp1", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=3.0))
    assert n.ip_link_capacity_gbps("ip1") == 200.0
    n.set_lightpath_mode("lp1", "100G-QPSK")
    n.set_qot_state("lp1", QoTState(gsnr_db=17.0, osnr_db=19.0, margin_db=5.0))
    assert n.ip_link_capacity_gbps("ip1") == 100.0


def test_capacity_zero_when_margin_negative():
    n = _model_with_lightpath("200G-16QAM")
    n.set_qot_state("lp1", QoTState(gsnr_db=18.0, osnr_db=20.0, margin_db=-0.5))
    assert n.ip_link_capacity_gbps("ip1") == 0.0


def test_capacity_raises_without_qot_state():
    n = _model_with_lightpath("200G-16QAM")
    with pytest.raises(LookupError, match="no QoT state"):
        n.ip_link_capacity_gbps("ip1")
