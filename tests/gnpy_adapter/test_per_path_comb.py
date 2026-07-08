"""Step C2 (S4-6 + S8-5): recompute builds each lightpath's interferer comb from
its OWN OMS, not a global concat of every committed channel.

Two consequences of the old global comb are fixed:
  - over-count: a channel on a disjoint fiber was counted as a co-propagating
    interferer, inflating NLI on paths it never shares;
  - malformed NLI: two lightpaths reusing a wavelength on disjoint OMS produced
    two carriers at the same frequency in one SpectralInformation
    (slot_width = f[1]-f[0] = 0).
"""
from multilayer_optical_mcp.model.assets import Lightpath, TransceiverMode
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.whatif import loading_from_model
from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading

MODE = "400G@7.1dB"


def _diamond_model():
    """A-M-Z (route1) and A-N-Z (route2): two node-disjoint routes sharing only
    the add/drop ROADMs at A and Z. Every OMS has a unique (src,dst)."""
    mode = TransceiverMode(id=MODE, bitrate_gbps=400.0, required_gsnr_db=7.1,
                           symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)
    graph = {
        "nodes": [{"id": "A"}, {"id": "M"}, {"id": "N"}, {"id": "Z"}],
        "edges": [
            {"src": "A", "dst": "M", "length_km": 80.0},
            {"src": "M", "dst": "Z", "length_km": 80.0},
            {"src": "A", "dst": "N", "length_km": 80.0},
            {"src": "N", "dst": "Z", "length_km": 80.0},
        ],
    }
    return model_from_abstract_graph(graph, modes=ModeRegistry([mode]))


ROUTE1 = ("oms_A_M", "oms_M_Z")
ROUTE2 = ("oms_A_N", "oms_N_Z")


def _gsnr_of_lp1(add_disjoint_lp2: bool, lp2_freq_hz: float = 193.5e12) -> float:
    model = _diamond_model()
    model.add_lightpath(Lightpath(id="lp1", oms_sequence=ROUTE1,
                                  mode_id=MODE, center_freq_hz=193.4e12))
    if add_disjoint_lp2:
        model.add_lightpath(Lightpath(id="lp2", oms_sequence=ROUTE2,
                                      mode_id=MODE, center_freq_hz=lp2_freq_hz))
    recompute_qot_under_loading(model=model, store=QoTResultStore(),
                                loading=loading_from_model(model))
    return model.get_qot_state("lp1").gsnr_db


def test_disjoint_fiber_lightpath_is_not_an_interferer():
    # 193.7 THz keeps lp2 clear of the single-carrier dummy (probe + 100 GHz =
    # 193.5), so the old global comb's phantom interferer is genuinely exercised.
    g_alone = _gsnr_of_lp1(add_disjoint_lp2=False)
    g_with = _gsnr_of_lp1(add_disjoint_lp2=True, lp2_freq_hz=193.7e12)
    assert abs(g_with - g_alone) < 1e-6, (
        f"a lightpath on a disjoint fiber must not change lp1's QoT: "
        f"alone={g_alone:.6f} with-disjoint={g_with:.6f} dB"
    )


def test_wavelength_reuse_on_disjoint_fiber_gives_clean_single_carrier():
    # lp2 reuses lp1's exact wavelength on the disjoint route -> the old global
    # concat would emit two 193.4 THz carriers (malformed NLI). Per-path comb
    # keeps lp1 a clean single carrier equal to the isolated case.
    g_alone = _gsnr_of_lp1(add_disjoint_lp2=False)
    g_reuse = _gsnr_of_lp1(add_disjoint_lp2=True, lp2_freq_hz=193.4e12)
    assert abs(g_reuse - g_alone) < 1e-6, (
        f"wavelength reuse on a disjoint fiber must not corrupt lp1's QoT: "
        f"alone={g_alone:.6f} reuse={g_reuse:.6f} dB"
    )
