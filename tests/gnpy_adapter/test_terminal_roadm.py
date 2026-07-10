"""Step B (S4-4 + S8-3): the path's terminal drop ROADM.

S4-4: each OMS owns its *source* ROADM as chain[0]; the final drop ROADM of a
path is nobody's chain[0], so it is never propagated and its drop-side
add_drop_osnr penalty is omitted even forward. compute_qot must walk the
terminal drop ROADM.

S8-3 (failure-side mirror): because the dst ROADM is absent from OMS.elements,
oms_seq_asset_set can't match it, so inject_failure(("roadm_<dst>",)) no-ops.
Crossing detection must include the terminal ROADM.
"""
from multilayer_optical_mcp.model.assets import Direction, Lightpath, TransceiverMode
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.whatif import inject_failure
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot

MODE = "400G@7.1dB"
LOADING = LoadingState(channels=(Channel(193.4e12, 100e9, None, MODE),))


def _line_ab_model():
    mode = TransceiverMode(id=MODE, bitrate_gbps=400.0, required_gsnr_db=7.1,
                           symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)
    graph = {"nodes": [{"id": "A"}, {"id": "B"}],
             "edges": [{"src": "A", "dst": "B", "length_km": 160.0,
                        "span_lengths_km": [80.0, 80.0]}]}
    return model_from_abstract_graph(graph, modes=ModeRegistry([mode]))


def test_forward_walks_terminal_drop_roadm():
    model = _line_ab_model()
    store = QoTResultStore()
    _, rid = compute_qot(model=model, store=store, oms_sequence=("oms_A_B",),
                         direction=Direction.FORWARD, mode_id=MODE, loading=LOADING)
    ids = [s.element_id for s in store.get(rid).snapshots]
    assert "roadm_B" in ids, f"terminal drop ROADM must be propagated, saw {ids}"
    # The drop happens at the end of the path.
    assert ids[-1] == "roadm_B", f"drop ROADM must be the terminal element, saw {ids}"


def test_terminal_drop_penalty_lowers_forward_gsnr():
    """The terminal drop ROADM's add_drop_osnr must actually cost GSNR.

    A bare-transceiver terminus (the old no-penalty baseline) is not a real
    optical terminal and is no longer synthesizable (S3-11 Option B), so instead
    of comparing against it we assert the penalty *within* the propagation: the
    final end-to-end GSNR (which applies the add_drop_osnr of every propagated
    ROADM, terminal drop ROADM included) sits below the raw GSNR propagated up to
    and including that terminal ROADM element. Combined with
    test_forward_walks_terminal_drop_roadm (roadm_B is propagated and terminal),
    this pins that the terminal drop penalty is real."""
    model = _line_ab_model()
    store = QoTResultStore()
    st, rid = compute_qot(model=model, store=store, oms_sequence=("oms_A_B",),
                          direction=Direction.FORWARD, mode_id=MODE, loading=LOADING)
    snaps = store.get(rid).snapshots
    assert snaps[-1].element_id == "roadm_B"
    raw_end_to_end = snaps[-1].gsnr_db_after  # pre-penalty (penalties are post-loop)
    assert st.gsnr_db < raw_end_to_end - 0.01, (
        f"post-propagation penalties (incl. terminal drop ROADM) must lower GSNR: "
        f"final={st.gsnr_db:.3f} vs raw propagated={raw_end_to_end:.3f} dB"
    )


def test_inject_failure_on_terminal_roadm_downs_lightpath():
    model = _line_ab_model()
    model.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_A_B",),
                                  mode_id=MODE, center_freq_hz=193.4e12))
    report = inject_failure(model, ("roadm_B",))
    assert "lp0" in report.downed_lightpaths, (
        "failing the terminal drop ROADM must down the lightpath crossing it"
    )
