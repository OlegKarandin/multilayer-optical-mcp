"""Step A (S4-2 + S4-3): compute_qot BACKWARD must walk the physically separate
reverse OMS (oms_<dst>_<src>) in natural order, not reversed(forward uids).

The importer builds two independent directed OMS per link, each with its own amp
chain (amp_<src>_<dst>_* vs amp_<dst>_<src>_*). Backward QoT must therefore see
the reverse chain's impairments — the asymmetric-degradation feature CLAUDE.md
promises. A single-OMS legacy model with no reverse counterpart still falls back
to reversed(uids).
"""
import math

from multilayer_optical_mcp.model.assets import Direction, TransceiverMode
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot

MODE = "400G@7.1dB"
LOADING = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, MODE),))


def _line_ab_model():
    mode = TransceiverMode(id=MODE, bitrate_gbps=400.0, required_gsnr_db=7.1,
                           symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)
    graph = {"nodes": [{"id": "A"}, {"id": "B"}],
             "edges": [{"src": "A", "dst": "B", "length_km": 160.0,
                        "span_lengths_km": [80.0, 80.0]}]}
    return model_from_abstract_graph(graph, modes=ModeRegistry([mode]))


def _gsnr(model, store, direction):
    st, _ = compute_qot(model=model, store=store, oms_sequence=("oms_A_B",),
                        direction=direction, mode_id=MODE, loading=LOADING)
    return st.gsnr_db


def test_reverse_chain_nf_moves_backward_qot_only():
    model = _line_ab_model()
    store = QoTResultStore()
    fwd0 = _gsnr(model, store, Direction.FORWARD)
    bwd0 = _gsnr(model, store, Direction.BACKWARD)

    # Degrade an amplifier that lives ONLY on the reverse chain (oms_B_A).
    model.apply_nf_delta("amp_B_A_0", 5.0)

    fwd1 = _gsnr(model, store, Direction.FORWARD)
    bwd1 = _gsnr(model, store, Direction.BACKWARD)

    assert bwd1 < bwd0 - 0.1, (
        f"reverse-chain NF must degrade backward QoT: {bwd0:.3f} -> {bwd1:.3f} dB"
    )
    assert abs(fwd1 - fwd0) < 1e-6, (
        f"forward QoT must be untouched by reverse-chain NF: {fwd0:.3f} -> {fwd1:.3f} dB"
    )


def test_backward_breakdown_references_reverse_oms_elements():
    model = _line_ab_model()
    store = QoTResultStore()
    _, rid = compute_qot(model=model, store=store, oms_sequence=("oms_A_B",),
                         direction=Direction.BACKWARD, mode_id=MODE, loading=LOADING)
    ids = {s.element_id for s in store.get(rid).snapshots}
    assert any(e.startswith("amp_B_A") for e in ids), (
        f"backward must walk reverse-OMS amps, saw {sorted(ids)}"
    )
    assert not any(e.startswith("amp_A_B") for e in ids), (
        f"backward must NOT walk forward-OMS amps, saw {sorted(ids)}"
    )
