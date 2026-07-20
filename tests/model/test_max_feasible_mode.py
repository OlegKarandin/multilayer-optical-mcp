"""whatif_max_feasible_mode: advisory read of current-vs-highest-feasible mode.

Reads recorded QoT (no GNPy calls). Maps each lightpath's delivered GSNR to the
highest-bitrate mode it could carry, and classifies the direction relative to the
frozen current mode. Advisory only — validate_plan's MODE_INFEASIBLE stays the
commit gate; this view never mutates and never blocks.
"""
from multilayer_optical_mcp.model.assets import (
    OMS, ROADM, TransceiverMode, Lightpath,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.whatif import max_feasible_mode_view


def _model() -> NetworkModel:
    modes = ModeRegistry([
        TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=15.0,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
        TransceiverMode(id="200G", bitrate_gbps=200.0, required_gsnr_db=10.0,
                        symbol_rate_baud=43.75e9, channel_spacing_hz=100e9),
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=5.0,
                        symbol_rate_baud=21.875e9, channel_spacing_hz=100e9),
    ])
    m = NetworkModel(modes=modes)
    m.add_roadm(ROADM(id="roadm_A"))
    m.add_roadm(ROADM(id="roadm_Z"))
    m.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="Z", elements=("roadm_A",)))
    return m


def _add_lp(m: NetworkModel, lp_id: str, mode_id: str, gsnr_db: float | None):
    m.add_lightpath(Lightpath(id=lp_id, oms_sequence=("oms1",), mode_id=mode_id,
                              center_freq_hz=193.4e12))
    if gsnr_db is not None:
        req = m.modes.get(mode_id).required_gsnr_db
        m.set_qot_state(lp_id, QoTState(gsnr_db=gsnr_db, osnr_db=gsnr_db,
                                        margin_db=gsnr_db - req))


def test_max_feasible_mode_classifies_all_directions():
    m = _model()
    _add_lp(m, "lp_headroom", "100G", gsnr_db=16.0)   # could reach 400G
    _add_lp(m, "lp_match", "400G", gsnr_db=16.0)      # already at the ceiling
    _add_lp(m, "lp_downshift", "400G", gsnr_db=11.0)  # 400G infeasible; 200G is best
    _add_lp(m, "lp_infeasible", "400G", gsnr_db=3.0)  # below every mode
    _add_lp(m, "lp_no_qot", "200G", gsnr_db=None)     # no recorded QoT -> omitted

    rows = {r.lightpath_id: r for r in max_feasible_mode_view(m)}

    assert "lp_no_qot" not in rows                    # omitted, never defaulted

    assert rows["lp_headroom"].max_feasible_mode == "400G"
    assert rows["lp_headroom"].direction == "headroom"

    assert rows["lp_match"].max_feasible_mode == "400G"
    assert rows["lp_match"].direction == "match"

    assert rows["lp_downshift"].max_feasible_mode == "200G"
    assert rows["lp_downshift"].direction == "downshift"

    assert rows["lp_infeasible"].max_feasible_mode is None
    assert rows["lp_infeasible"].direction == "infeasible"

    # current_mode is always the frozen mode, unchanged by the view
    assert rows["lp_downshift"].current_mode == "400G"
