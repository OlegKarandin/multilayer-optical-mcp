"""Gate: a model synthesized to match toy_2span must reproduce load_toy GSNR."""
from pathlib import Path

from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import (
    Amplifier, Direction, Fiber, FiberType, OMS, ROADM, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot_results import QoTResultStore

REPO_ROOT = Path(__file__).resolve().parents[2]
TOY = REPO_ROOT / "topologies" / "toy_2span.json"
MODE = "400G@7.1dB"
TOL_DB = 0.25


def _mode():
    return TransceiverMode(id=MODE, bitrate_gbps=400.0, required_gsnr_db=7.1,
                           symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)


def _toy_model_synthesized() -> NetworkModel:
    """Model whose synthesized GNPy network mirrors toy_2span.json.

    Uses fresh UIDs (not the legacy toy_2span names) so build_gnpy_network
    constructs the GNPy graph from scratch without loading a topology file.
    The amplifier chain matches toy_2span.json exactly:
      ROADM -> booster (gain=20 dB) -> fiber(80 km) -> ILA (gain=20 dB)
            -> fiber(80 km) -> preamp (gain=20 dB)

    No explicit transceivers are registered; the OMS src/dst node IDs become
    synthetic Transceiver elements in the GNPy graph (same as test_compute_qot
    pattern and toy_2span.json which has no paired Z-side ROADM).
    """
    n = NetworkModel(modes=ModeRegistry([_mode()]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_roadm(ROADM(id="roadm_A"))
    n.add_amplifier(Amplifier(id="amp_booster", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber_0", a_end="roadm_A", z_end="amp_ila",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp_ila", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber_1", a_end="amp_ila", z_end="amp_preamp",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp_preamp", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms_syn", src_node_id="trx_A", dst_node_id="trx_Z", elements=(
        "roadm_A", "amp_booster", "fiber_0", "amp_ila",
        "fiber_1", "amp_preamp")))
    return n


def _toy_model_legacy() -> NetworkModel:
    """Model using toy_2span.json UIDs so compute_qot(topo_path=TOY) resolves them."""
    n = NetworkModel(modes=ModeRegistry([_mode()]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_roadm(ROADM(id="ROADM A"))
    n.add_amplifier(Amplifier(id="booster A", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="east fiber A to ILA", a_end="ROADM A",
                      z_end="east edfa in ILA", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="east edfa in ILA", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="east fiber ILA to Z", a_end="east edfa in ILA",
                      z_end="east edfa at Z", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="east edfa at Z", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms_leg", src_node_id="trx A", dst_node_id="trx Z",
                  elements=(
                      "ROADM A", "booster A",
                      "east fiber A to ILA", "east edfa in ILA",
                      "east fiber ILA to Z", "east edfa at Z",
                  )))
    return n


def _gsnr_synthesized() -> float:
    model = _toy_model_synthesized()
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, MODE),))
    state, _ = compute_qot(model=model, store=store, oms_sequence=("oms_syn",),
                           direction=Direction.FORWARD, mode_id=MODE, loading=loading)
    return state.gsnr_db


def _gsnr_legacy() -> float:
    """GSNR from the file-loaded toy_2span.json via topo_path."""
    model = _toy_model_legacy()
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, MODE),))
    state, _ = compute_qot(model=model, store=store, oms_sequence=("oms_leg",),
                           direction=Direction.FORWARD, mode_id=MODE, loading=loading,
                           topo_path=TOY)
    return state.gsnr_db


def test_synthesized_toy_matches_file_loaded_toy():
    g_syn = _gsnr_synthesized()
    g_leg = _gsnr_legacy()
    assert abs(g_syn - g_leg) < TOL_DB, (
        f"synthesized GSNR {g_syn:.3f} dB diverges from file-loaded {g_leg:.3f} dB"
        f" (delta={g_syn - g_leg:+.3f} dB, tolerance={TOL_DB} dB)"
    )
