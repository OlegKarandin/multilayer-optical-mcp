"""S1-4: add_lightpath must reject an OMS sequence that does not physically
chain (each OMS's dst_node must equal the next OMS's src_node). A gap or
inversion otherwise passes silently and surfaces only at propagation time."""
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel


def _base() -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="x", z_end="y", length_km=80.0,
                      type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    els = ("amp1", "f1", "amp2")
    n.add_oms(OMS(id="oms_AB", src_node_id="A", dst_node_id="B", elements=els))
    n.add_oms(OMS(id="oms_BC", src_node_id="B", dst_node_id="C", elements=els))
    n.add_oms(OMS(id="oms_CD", src_node_id="C", dst_node_id="D", elements=els))
    return n


def test_add_lightpath_accepts_chained_oms():
    n = _base()
    n.add_lightpath(Lightpath(id="lp", oms_sequence=("oms_AB", "oms_BC"),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    assert n.get_lightpath("lp").oms_sequence == ("oms_AB", "oms_BC")


def test_add_lightpath_rejects_gap_in_oms_chain():
    n = _base()
    # oms_AB ends at B, oms_CD starts at C -> gap.
    with pytest.raises(ValueError):
        n.add_lightpath(Lightpath(id="lp", oms_sequence=("oms_AB", "oms_CD"),
                                  mode_id="100G-QPSK", center_freq_hz=193.4e12))


def test_add_lightpath_rejects_inverted_oms():
    n = _base()
    # oms_BC (B->C) then oms_AB (A->B): C != A -> inverted.
    with pytest.raises(ValueError):
        n.add_lightpath(Lightpath(id="lp", oms_sequence=("oms_BC", "oms_AB"),
                                  mode_id="100G-QPSK", center_freq_hz=193.4e12))
