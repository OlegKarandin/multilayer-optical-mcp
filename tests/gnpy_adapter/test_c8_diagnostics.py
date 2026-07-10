"""Batch C8 — Stage 4 diagnostics & guards (Steps E, F, G).

Step E (S4-1/A6): the single-channel dummy is placed at ``probe + 100 GHz``. Near
the top of the SI band that lands out of band, so it must be placed *below* the
probe instead — while keeping the loading frequency-ascending (the ``probe_idx``
alignment invariant that ``_per_path_loading`` documents).

Step F (S4-7/A5): the limiting-element diagnostic takes ``min`` over GSNR deltas
*including* the first-noise ``inf → finite`` transition (``finite - inf = -inf``),
so the first amplifier always wins and ``limiting_element_id`` is meaningless. The
limiting element must be selected from finite (real degradation) deltas only. The
field is live tool-surface (server.compute_qot / recompute / breakdown + views),
so it is fixed to be correct, not dropped.

Step G (S4-8): verification-only — a Transceiver predecessor of the first path
element must exist for both the synthesized and the file-loaded toy topologies,
else ``si`` enters the chain without tx_power initialisation.
"""
import math

from gnpy.core.elements import Transceiver as GnpyTrx

from multilayer_optical_mcp.gnpy_adapter.adapter import (
    _SI_F_MAX_HZ,
    _ensure_min_two_channels,
    _find_launch_transceiver,
    compute_qot,
)
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.synthesize import build_gnpy_network
from multilayer_optical_mcp.gnpy_adapter.translate import (
    DEFAULT_EQPT,
    load_toy,
    resolve_oms_path_to_uids,
)
from multilayer_optical_mcp.model.assets import Direction, TransceiverMode
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel  # noqa: F401
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph

from tests.gnpy_adapter.test_ground_truth_bridge import TOY

MODE = "400G@7.1dB"


def _line_ab_model() -> "NetworkModel":
    """Importer-built A→B path with two 80 km spans (real amp chain + drop ROADM)."""
    mode = TransceiverMode(id=MODE, bitrate_gbps=400.0, required_gsnr_db=7.1,
                           symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)
    graph = {"nodes": [{"id": "A"}, {"id": "B"}],
             "edges": [{"src": "A", "dst": "B", "length_km": 160.0,
                        "span_lengths_km": [80.0, 80.0]}]}
    return model_from_abstract_graph(graph, modes=ModeRegistry([mode]))


# ----------------------------------------------------------------- Step E
def test_mid_band_dummy_is_placed_above_probe():
    """Regression: away from the band edge the dummy stays at probe + 100 GHz."""
    probe_freq = 193.4e12
    probe = Channel(probe_freq, 100e9, None, MODE)
    result = _ensure_min_two_channels(LoadingState((probe,)), probe_freq)
    freqs = [c.center_freq_hz for c in result.channels]
    assert freqs == [probe_freq, probe_freq + 100e9]


def test_band_edge_dummy_is_placed_below_and_stays_in_band():
    """A probe at the top of the SI band must not push the dummy out of band."""
    probe_freq = _SI_F_MAX_HZ  # top grid slot (196.1 THz)
    probe = Channel(probe_freq, 100e9, None, MODE)
    result = _ensure_min_two_channels(LoadingState((probe,)), probe_freq)
    freqs = [c.center_freq_hz for c in result.channels]
    assert len(freqs) == 2
    # The dummy must remain within the SI band, i.e. not exceed the upper edge.
    assert max(freqs) <= _SI_F_MAX_HZ
    # Channels must stay frequency-ascending so the frequency-resolved probe_idx
    # aligns with gnpy's SI arrays (the _per_path_loading invariant).
    assert freqs == sorted(freqs)
    # The probe itself must survive unchanged.
    assert probe_freq in freqs


# ----------------------------------------------------------------- Step F
def test_limiting_element_excludes_first_noise_transition():
    """The limiting element must be a real finite→finite degradation.

    Under the bug the first amplifier's ``inf → finite`` delta (= ``-inf``) always
    wins the ``min`` and is reported as limiting. The corrected diagnostic ignores
    the infinite first-noise transition and picks a finite-delta element.
    """
    model = _line_ab_model()
    store = QoTResultStore()
    loading = LoadingState((Channel(193.4e12, 100e9, None, MODE),))
    state, rid = compute_qot(model=model, store=store, oms_sequence=("oms_A_B",),
                             direction=Direction.FORWARD, mode_id=MODE, loading=loading)
    bd = store.get(rid)
    assert state.limiting_element_id is not None
    chosen = next(s for s in bd.snapshots
                  if s.element_id == state.limiting_element_id)
    assert math.isfinite(chosen.gsnr_delta_db), (
        f"limiting element {state.limiting_element_id!r} has non-finite delta "
        f"{chosen.gsnr_delta_db} — the first-noise inf transition leaked in"
    )


# ----------------------------------------------------------------- Step G
def test_launch_transceiver_found_on_synthesized_topology():
    model = _line_ab_model()
    _eqpt, network = build_gnpy_network(model)
    uids = list(resolve_oms_path_to_uids(model, ("oms_A_B",)))
    by_uid = {n.uid: n for n in network.nodes}
    trx = _find_launch_transceiver(network, uids, by_uid)
    assert isinstance(trx, GnpyTrx), "synthesized path must have a launch Transceiver"


def test_launch_transceiver_found_on_toy_json_topology():
    _eqpt, network = load_toy(eqpt_path=DEFAULT_EQPT, topo_path=TOY)
    by_uid = {n.uid: n for n in network.nodes}
    # The toy path's first element is the add ROADM; its Transceiver predecessor
    # is the launch transponder ("trx A" in toy_2span.json).
    trx = _find_launch_transceiver(network, ["ROADM A"], by_uid)
    assert isinstance(trx, GnpyTrx), "toy path must have a launch Transceiver"
    assert trx.uid == "trx A"
