"""S1-5: when a NetworkModel carries a SpectrumGrid, add_lightpath validates
center_freq_hz against it at add-time (via grid.slot_of) instead of letting an
off-grid frequency surface only at build_spectrum_state (routing time)."""
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.spectrum import SpectrumGrid


def _base(grid=None) -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]), grid=grid)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="x", z_end="y", length_km=80.0,
                      type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "amp1", "f1", "amp2")))
    return n


def test_add_lightpath_accepts_on_grid_frequency():
    n = _base(grid=SpectrumGrid.default())
    # 193.4 THz is slot 20 on the default grid.
    n.add_lightpath(Lightpath(id="lp", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    assert n.get_lightpath("lp").center_freq_hz == 193.4e12


def test_add_lightpath_rejects_off_grid_frequency():
    n = _base(grid=SpectrumGrid.default())
    # 200 THz is far above the top of the default grid (196.1 THz).
    with pytest.raises(ValueError):
        n.add_lightpath(Lightpath(id="lp", oms_sequence=("oms1",),
                                  mode_id="100G-QPSK", center_freq_hz=200.0e12))


def test_add_lightpath_skips_grid_check_when_no_grid():
    # Backward compatible: no grid => no add-time frequency validation.
    n = _base(grid=None)
    n.add_lightpath(Lightpath(id="lp", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=200.0e12))
    assert n.get_lightpath("lp").center_freq_hz == 200.0e12
