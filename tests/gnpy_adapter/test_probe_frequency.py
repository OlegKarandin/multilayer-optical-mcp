"""Step C1 (S4-5): the probe channel is selected by center frequency, not by the
first mode_id match.

In any WDM load with several same-mode channels, the old code evaluated *every*
same-mode lightpath at the first matching channel's frequency, so all same-mode
lightpaths returned identical QoT. compute_qot / recompute_qot_under_loading
must probe each lightpath at its own center_freq_hz.
"""
from multilayer_optical_mcp.model.assets import Direction, Lightpath, TransceiverMode
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.adapter import (
    compute_qot, recompute_qot_under_loading,
)

MODE = "400G@7.1dB"

# Three same-mode channels: the middle one (193.4) has two NLI neighbors, the
# edge one (193.3) has a single neighbor -> the center channel has more NLI and
# therefore a lower GSNR.
THREE = LoadingState(channels=(
    Channel(193.3e12, 100e9, 0.0, MODE),
    Channel(193.4e12, 100e9, 0.0, MODE),
    Channel(193.5e12, 100e9, 0.0, MODE),
))


def _line_ab_model():
    mode = TransceiverMode(id=MODE, bitrate_gbps=400.0, required_gsnr_db=7.1,
                           symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)
    graph = {"nodes": [{"id": "A"}, {"id": "B"}],
             "edges": [{"src": "A", "dst": "B", "length_km": 160.0,
                        "span_lengths_km": [80.0, 80.0]}]}
    return model_from_abstract_graph(graph, modes=ModeRegistry([mode]))


def test_probe_selected_by_frequency():
    model = _line_ab_model()
    store = QoTResultStore()

    def qot_at(freq):
        st, _ = compute_qot(model=model, store=store, oms_sequence=("oms_A_B",),
                            direction=Direction.FORWARD, mode_id=MODE,
                            loading=THREE, center_freq_hz=freq)
        return st.gsnr_db

    edge = qot_at(193.3e12)
    center = qot_at(193.4e12)
    # The center channel carries more NLI than the edge channel -> lower GSNR.
    assert center < edge - 1e-4, (
        f"center-channel probe {center:.5f} must be worse than edge {edge:.5f} dB;"
        f" equality means the probe ignored center_freq_hz"
    )


def test_recompute_probes_each_lightpath_at_its_own_frequency():
    # Three lightpaths on the same OMS. recompute must probe each at its OWN
    # frequency: the center channel (193.4, both neighbors adjacent) carries more
    # NLI than the edge channel (193.3, one neighbor at +100 GHz, one at +200).
    model = _line_ab_model()
    for lid, f in (("lp_edge", 193.3e12), ("lp_center", 193.4e12), ("lp_hi", 193.5e12)):
        model.add_lightpath(Lightpath(id=lid, oms_sequence=("oms_A_B",),
                                      mode_id=MODE, center_freq_hz=f))
    recompute_qot_under_loading(model=model, store=QoTResultStore(), loading=THREE)

    g_edge = model.get_qot_state("lp_edge").gsnr_db
    g_center = model.get_qot_state("lp_center").gsnr_db
    assert g_center < g_edge - 1e-4, (
        f"lightpaths must be probed at their own frequency: center {g_center:.5f} "
        f"should be worse than edge {g_edge:.5f} dB (equal => same-freq bug)"
    )
