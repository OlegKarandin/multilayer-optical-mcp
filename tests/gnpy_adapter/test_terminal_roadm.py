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
LOADING = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, MODE),))


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
    """The terminal drop ROADM's add_drop_osnr must actually cost GSNR: a path
    terminating at a ROADM is worse than the identical span terminating at a bare
    transceiver (no drop penalty)."""
    model = _line_ab_model()
    store = QoTResultStore()
    st_roadm, _ = compute_qot(model=model, store=store, oms_sequence=("oms_A_B",),
                              direction=Direction.FORWARD, mode_id=MODE, loading=LOADING)

    # Identical physical span but the OMS drops into a bare transceiver (no ROADM
    # named roadm_<dst>), so there is no terminal drop penalty.
    trx_model = _line_ab_model()
    oms = trx_model.get_oms("oms_A_B")
    from dataclasses import replace
    trx_model._oms["oms_A_B"] = replace(oms, dst_node_id="trx_end")
    st_trx, _ = compute_qot(model=trx_model, store=QoTResultStore(),
                            oms_sequence=("oms_A_B",),
                            direction=Direction.FORWARD, mode_id=MODE, loading=LOADING)

    assert st_roadm.gsnr_db < st_trx.gsnr_db - 0.01, (
        f"drop-ROADM path {st_roadm.gsnr_db:.3f} must be worse than trx-terminated "
        f"{st_trx.gsnr_db:.3f} dB by the add_drop_osnr penalty"
    )


def test_inject_failure_on_terminal_roadm_downs_lightpath():
    model = _line_ab_model()
    model.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_A_B",),
                                  mode_id=MODE, center_freq_hz=193.4e12))
    report = inject_failure(model, ("roadm_B",))
    assert "lp0" in report.downed_lightpaths, (
        "failing the terminal drop ROADM must down the lightpath crossing it"
    )
