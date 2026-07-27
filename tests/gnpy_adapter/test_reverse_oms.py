"""Step A (S4-2 + S4-3): compute_qot BACKWARD must walk the physically separate
reverse OMS (oms_<dst>_<src>) in natural order, not reversed(forward uids).

The importer builds two independent directed OMS per link, each with its own amp
chain (amp_<src>_<dst>_* vs amp_<dst>_<src>_*). Backward QoT must therefore see
the reverse chain's impairments — the asymmetric-degradation feature CLAUDE.md
promises. A single-OMS legacy model with no reverse counterpart still falls back
to reversed(uids).
"""
import math
import pytest

from multilayer_optical_mcp.model.assets import (
    Direction, TransceiverMode, Amplifier, Fiber, FiberType, OMS, ROADM,
    Transceiver
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
from multilayer_optical_mcp.gnpy_adapter.translate import reverse_oms_sequence

MODE = "400G@7.1dB"
LOADING = LoadingState(channels=(Channel(193.4e12, 100e9, None, MODE),))


def _mode():
    """Helper to create a test mode."""
    return TransceiverMode(id=MODE, bitrate_gbps=400.0, required_gsnr_db=7.1,
                           symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)


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


def test_reverse_oms_sequence_raises_on_ambiguous_parallel_routes():
    """Regression for the audit's reverse-OMS Critical finding: when two
    distinct OMS share the same ordered (src, dst) node pair (parallel routes
    between the same two sites), reverse_oms_sequence must raise rather than
    silently returning whichever one happens to be last in model.list_oms()."""
    n = NetworkModel(modes=ModeRegistry([_mode()]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
        n.add_transceiver(Transceiver(id=f"trx_{node}", site=node))

    def _span(tag, src, dst):
        n.add_amplifier(Amplifier(id=f"boost_{tag}", type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
        n.add_fiber(Fiber(id=f"f_{tag}", a_end=f"roadm_{src}", z_end=f"pre_{tag}",
                          length_km=80.0, type_variety="SSMF"))
        n.add_amplifier(Amplifier(id=f"pre_{tag}", type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
        n.add_oms(OMS(id=tag, src_node_id=src, dst_node_id=dst,
                      elements=(f"roadm_{src}", f"boost_{tag}", f"f_{tag}", f"pre_{tag}")))

    # Two PARALLEL forward routes A->B, and their own correctly-paired reverses.
    _span("route1_AB", "A", "B")
    _span("route1_BA", "B", "A")
    _span("route2_AB", "A", "B")
    _span("route2_BA", "B", "A")

    with pytest.raises(ValueError, match="ambiguous"):
        reverse_oms_sequence(n, ("route1_AB",))
    with pytest.raises(ValueError, match="ambiguous"):
        reverse_oms_sequence(n, ("route2_AB",))


def test_reverse_oms_sequence_still_resolves_unambiguous_pair():
    """Regression guard: the common (non-parallel) case must still work."""
    n = NetworkModel(modes=ModeRegistry([_mode()]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
        n.add_transceiver(Transceiver(id=f"trx_{node}", site=node))
    n.add_amplifier(Amplifier(id="boost_AB", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f_AB", a_end="roadm_A", z_end="pre_AB",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="pre_AB", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms_AB", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "boost_AB", "f_AB", "pre_AB")))
    n.add_amplifier(Amplifier(id="boost_BA", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f_BA", a_end="roadm_B", z_end="pre_BA",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="pre_BA", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms_BA", src_node_id="B", dst_node_id="A",
                  elements=("roadm_B", "boost_BA", "f_BA", "pre_BA")))

    assert reverse_oms_sequence(n, ("oms_AB",)) == ("oms_BA",)
