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
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, None, MODE),))
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


# ---------------------------------------------------------------------------
# Task 4: inject_failure + FailureReport
# ---------------------------------------------------------------------------

import math as _math
from multilayer_optical_mcp.model.whatif import inject_failure, FailureReport


def test_inject_failure_downs_crossing_lightpath():
    m = _model_with_two_lightpaths()  # lp0 rides oms_0_1, lp1 rides oms_1_0
    report = inject_failure(m, ("fiber_0_1_0",))  # a fiber on oms_0_1 only
    # lp0 crosses the failed fiber -> sentinel; lp1 does not -> untouched
    assert _math.isinf(m.get_qot_state("lp0").margin_db)
    assert m.get_qot_state("lp0").margin_db < 0
    assert m.get_qot_state("lp1").margin_db == 0.9
    assert "lp0" in report.downed_lightpaths
    assert "lp1" not in report.downed_lightpaths


def test_inject_failure_records_failed_assets():
    m = _model_with_two_lightpaths()
    inject_failure(m, ("fiber_0_1_0",))
    assert m.is_failed("fiber_0_1_0")


# ---------------------------------------------------------------------------
# Task 5: inject_degradation + DegradationReport
# ---------------------------------------------------------------------------

import pytest
from multilayer_optical_mcp.model.whatif import inject_degradation, DegradationReport


def _live_model_one_lightpath():
    """A model whose lp0 has real (synthesized) QoT, near threshold."""
    m = _one_edge_model()
    m.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_0_1",),
                              mode_id=MODE, center_freq_hz=193.4e12))
    # seed real QoT
    from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading
    recompute_qot_under_loading(model=m, store=QoTResultStore(),
                                loading=loading_from_model(m))
    return m


def test_inject_degradation_lowers_margin_and_reports():
    m = _live_model_one_lightpath()
    before = m.get_qot_state("lp0").margin_db
    report = inject_degradation(m, store=QoTResultStore(), asset_id="amp_0_1_0",
                                nf_delta=6.0, loss_delta=0.0)
    after = m.get_qot_state("lp0").margin_db
    assert after < before  # +6 dB NF degrades GSNR -> lower margin
    row = next(r for r in report.rows if r.lightpath_id == "lp0")
    assert row.margin_before == before
    assert row.margin_after == after
    assert isinstance(report, DegradationReport)


def test_inject_degradation_unknown_asset_raises():
    m = _live_model_one_lightpath()
    with pytest.raises(KeyError):
        inject_degradation(m, store=QoTResultStore(), asset_id="nope", nf_delta=1.0)


# ---------------------------------------------------------------------------
# Batch C4 — What-if composition (Stage 8)
# ---------------------------------------------------------------------------

from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading


def test_recompute_does_not_resurrect_failed_lightpath():
    # S8-1: a recompute after inject_failure must NOT overwrite the -inf sentinel
    # with a feasible GSNR — the downed lightpath stays down.
    m = _live_model_one_lightpath()
    inject_failure(m, ("fiber_0_1_0",))  # a fiber on oms_0_1, which lp0 crosses
    assert math.isinf(m.get_qot_state("lp0").margin_db)

    recompute_qot_under_loading(model=m, store=QoTResultStore(),
                                loading=loading_from_model(m))

    st = m.get_qot_state("lp0")
    assert math.isinf(st.margin_db) and st.margin_db < 0
    assert not st.mode_feasible


def test_failure_and_degradation_compose():
    # S8-1 (composition): fail a lightpath, then degrade an amp on its path.
    # inject_degradation's internal recompute must leave the failed lightpath down.
    m = _live_model_one_lightpath()
    inject_failure(m, ("fiber_0_1_0",))
    inject_degradation(m, store=QoTResultStore(), asset_id="amp_0_1_0", nf_delta=1.0)
    assert m.get_qot_state("lp0").margin_db < 0  # stays down, not resurrected


def _model_with_infeasible_second_lightpath():
    """lp0 (feasible mode) + lp_hard (unreachably high required GSNR)."""
    reg = ModeRegistry([
        TransceiverMode(id=MODE, bitrate_gbps=400.0, required_gsnr_db=7.1,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
        TransceiverMode(id="HARD", bitrate_gbps=400.0, required_gsnr_db=99.0,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    ])
    m = model_from_abstract_graph({
        "nodes": [{"id": 0}, {"id": 1}],
        "edges": [{"src": 0, "dst": 1, "length_km": 160.0, "num_spans": 2,
                   "span_lengths_km": [80.0, 80.0], "fiber_type": "SSMF",
                   "amplifier_nf_db": [5.5, 5.5]}],
    }, modes=reg)
    m.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_0_1",),
                              mode_id=MODE, center_freq_hz=193.4e12))
    m.add_lightpath(Lightpath(id="lp_hard", oms_sequence=("oms_0_1",),
                              mode_id="HARD", center_freq_hz=193.5e12))
    return m


def test_no_crossing_without_feasible_baseline():
    # S8-2: lp_hard has no prior QoT and is infeasible after recompute. It must
    # NOT be reported as "crossed" (there was no feasible baseline to cross from).
    m = _model_with_infeasible_second_lightpath()
    m.set_qot_state("lp0", QoTState(gsnr_db=18.0, osnr_db=25.0, margin_db=10.9))

    report = inject_degradation(m, store=QoTResultStore(),
                                asset_id="amp_0_1_0", nf_delta=1.0)

    hard = next(r for r in report.rows if r.lightpath_id == "lp_hard")
    assert not hard.feasible_after       # GSNR ~18 << 99 required
    assert not hard.feasible_before      # no feasible baseline existed
    assert not hard.crossed
    assert "lp_hard" not in report.crossings


def test_feasibility_predicate_is_unified():
    # S8-4: feasible_before and feasible_after both mean "margin >= 0".
    m = _live_model_one_lightpath()
    report = inject_degradation(m, store=QoTResultStore(),
                                asset_id="amp_0_1_0", nf_delta=1.0)
    for r in report.rows:
        assert r.feasible_after == (r.margin_after >= 0)
        assert r.feasible_before == (r.margin_before >= 0)


def test_clear_failed_drops_stale_sentinel():
    # S8-6: clearing the failed asset must drop the -inf sentinel so the QoT
    # store and _failed_assets cannot disagree — capacity reads "unknown".
    m = _live_model_one_lightpath()
    inject_failure(m, ("fiber_0_1_0",))
    assert math.isinf(m.get_qot_state("lp0").margin_db)

    m.clear_failed(("fiber_0_1_0",))

    with pytest.raises(LookupError):
        m.get_qot_state("lp0")


def test_clear_failed_keeps_sentinel_while_another_asset_still_failed():
    # S8-6: a lightpath still crossing a remaining failed asset stays sentinelled.
    m = _live_model_one_lightpath()
    inject_failure(m, ("fiber_0_1_0", "amp_0_1_1"))  # both on oms_0_1
    m.clear_failed(("fiber_0_1_0",))                 # amp_0_1_1 still failed
    st = m.get_qot_state("lp0")
    assert math.isinf(st.margin_db) and st.margin_db < 0


# ---------------------------------------------------------------------------
# whatif_sensitivity
# ---------------------------------------------------------------------------

from multilayer_optical_mcp.model.whatif import whatif_sensitivity, SensitivityResult


def test_sensitivity_flags_the_perturbed_amp_as_dominant():
    """Perturbing one amp's NF on branch B must show up as the dominant
    per-element delta, with the OTHER amp on the same path near zero — proving
    the diff isolates the changed asset instead of echoing the cumulative
    downstream shift at every element."""
    model_a = _one_edge_model()
    model_b = model_a.clone()
    model_b.apply_nf_delta("amp_0_1_1", 6.0)   # second (downstream) amp only
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, None, MODE),))
    res = whatif_sensitivity(model_a, model_b, store=store,
                             oms_sequence=("oms_0_1",), direction=Direction.FORWARD,
                             mode_id=MODE, loading=loading)
    assert isinstance(res, SensitivityResult)
    assert res.delta_margin_db < -0.5   # margin genuinely moved
    top = res.rows[0]
    assert top.element_id == "amp_0_1_1"
    assert abs(top.gsnr_contribution_delta_db) > 1.0
    other = next(r for r in res.rows if r.element_id == "amp_0_1_0")
    assert abs(other.gsnr_contribution_delta_db) < 0.05   # unaffected amp ~= 0


def test_sensitivity_identical_branches_are_all_zero():
    model_a = _one_edge_model()
    model_b = model_a.clone()
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, None, MODE),))
    res = whatif_sensitivity(model_a, model_b, store=store,
                             oms_sequence=("oms_0_1",), direction=Direction.FORWARD,
                             mode_id=MODE, loading=loading)
    assert res.delta_margin_db == pytest.approx(0.0, abs=1e-9)
    assert all(abs(r.gsnr_contribution_delta_db) < 1e-9 for r in res.rows)
