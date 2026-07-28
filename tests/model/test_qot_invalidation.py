"""S1-7: physics mutations must invalidate stale QoT so ip_link_capacity_gbps
never silently serves a capacity read off a mode/impairment that changed."""
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, TransceiverMode, Router, IPLink,
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
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "amp1", "f1", "amp2")))
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


def test_add_lightpath_clears_qot_for_lightpath_sharing_the_oms():
    """Provisioning a new channel changes NLI for every channel already
    co-propagating on the same OMS -- lp1's recorded QoT is now stale and must
    read as unknown (LookupError), not the value recorded before lp2 was lit,
    until the next recompute repopulates it."""
    n = _model()
    assert n.get_qot_state("lp1").gsnr_db == 22.0  # sanity: recorded before lp2

    n.add_lightpath(Lightpath(id="lp2", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.5e12))

    with pytest.raises(LookupError):
        n.get_qot_state("lp1")
    with pytest.raises(LookupError):
        n.ip_link_capacity_gbps("ip1")


def test_remove_lightpath_clears_qot_for_remaining_lightpath_sharing_the_oms():
    """Tearing down a channel changes NLI for every surviving channel that was
    co-propagating with it -- the survivor's recorded QoT is stale and must
    read as unknown until the next recompute."""
    n = _model()
    n.add_lightpath(Lightpath(id="lp2", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.5e12))
    # lp2's addition above already invalidated lp1's QoT (see the add test) --
    # re-seed both so the removal below is the only invalidation under test.
    n.set_qot_state("lp1", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=4.0))
    n.set_qot_state("lp2", QoTState(gsnr_db=20.0, osnr_db=23.0, margin_db=2.0))

    n.remove_lightpath("lp2")

    with pytest.raises(LookupError):
        n.get_qot_state("lp1")
