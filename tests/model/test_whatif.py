import math
from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import Direction
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph

MODE = "400G@7.1dB"


def _reg():
    return ModeRegistry([TransceiverMode(id=MODE, bitrate_gbps=400.0,
        required_gsnr_db=7.1, symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)])


def _one_edge_model(extra_loss_db: float = 0.0):
    m = model_from_abstract_graph({
        "nodes": [{"id": 0}, {"id": 1}],
        "edges": [{"src": 0, "dst": 1, "length_km": 160.0, "num_spans": 2,
                   "span_lengths_km": [80.0, 80.0], "fiber_type": "SSMF",
                   "amplifier_nf_db": [5.5, 5.5]}],
    }, modes=_reg())
    if extra_loss_db:
        m.apply_loss_delta("fiber_0_1_0", extra_loss_db)
    return m


def _gsnr(m):
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, MODE),))
    st, _ = compute_qot(model=m, store=store, oms_sequence=("oms_0_1",),
                        direction=Direction.FORWARD, mode_id=MODE, loading=loading)
    return st.gsnr_db


def test_extra_loss_lowers_gsnr():
    base = _gsnr(_one_edge_model(0.0))
    degraded = _gsnr(_one_edge_model(4.0))
    assert degraded < base - 0.5  # 4 dB lumped loss is visible


def test_mark_failed_and_query():
    m = _one_edge_model()
    m.mark_failed(("fiber_0_1_0",))
    assert m.is_failed("fiber_0_1_0")
    assert not m.is_failed("fiber_0_1_1")
    assert m.failed_assets() == frozenset({"fiber_0_1_0"})


def test_apply_nf_delta_mutates_amp():
    m = _one_edge_model()
    before = m.get_amplifier("amp_0_1_0").nf_db
    m.apply_nf_delta("amp_0_1_0", 3.0)
    assert m.get_amplifier("amp_0_1_0").nf_db == before + 3.0


def test_failed_assets_isolated_on_branch(tmp_path):
    # branch isolation: marking failed on a branch must not touch the parent.
    from multilayer_optical_mcp.model.snapshots import SnapshotStore
    base = _one_edge_model()
    store = SnapshotStore(base)
    sid = store.create()
    bid = store.branch(sid)
    store.current().mark_failed(("fiber_0_1_0",))
    assert store.current().is_failed("fiber_0_1_0")
    assert not store.get(sid).is_failed("fiber_0_1_0")  # parent untouched


# ---------------------------------------------------------------------------
# Task 3: loading_from_model + margin_threshold_sweep
# ---------------------------------------------------------------------------

from multilayer_optical_mcp.model.whatif import (
    loading_from_model, margin_threshold_sweep, MarginSweepRow,
)
from multilayer_optical_mcp.model.assets import OMS, Lightpath
from multilayer_optical_mcp.model.qot import QoTState


def _model_with_two_lightpaths():
    m = _one_edge_model()
    # oms_0_1 and oms_1_0 are both created by model_from_abstract_graph
    m.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_0_1",),
                              mode_id=MODE, center_freq_hz=193.4e12))
    m.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms_1_0",),
                              mode_id=MODE, center_freq_hz=193.5e12))
    m.set_qot_state("lp0", QoTState(gsnr_db=9.0, osnr_db=20.0, margin_db=1.9))
    m.set_qot_state("lp1", QoTState(gsnr_db=8.0, osnr_db=19.0, margin_db=0.9))
    return m


def test_loading_from_model_one_channel_per_lightpath():
    m = _model_with_two_lightpaths()
    loading = loading_from_model(m)
    assert len(loading.channels) == 2
    freqs = sorted(c.center_freq_hz for c in loading.channels)
    assert freqs == [193.4e12, 193.5e12]


def test_sweep_returns_fragile_sorted_by_margin():
    m = _model_with_two_lightpaths()
    rows = margin_threshold_sweep(m, threshold_db=2.0)
    assert [r.lightpath_id for r in rows] == ["lp1", "lp0"]  # ascending margin
    assert all(isinstance(r, MarginSweepRow) for r in rows)


def test_sweep_excludes_well_margined():
    m = _model_with_two_lightpaths()
    rows = margin_threshold_sweep(m, threshold_db=1.0)
    assert [r.lightpath_id for r in rows] == ["lp1"]  # lp0 margin 1.9 > 1.0
