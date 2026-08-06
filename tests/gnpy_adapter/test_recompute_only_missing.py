"""`only_missing=True` is the scoping optimization that makes gating a
single-op mutation with a validate_plan-equivalent check viable cost-wise:
recompute_qot_under_loading should skip the real GNPy call for any lightpath
that already has a stored QoTState, since the model's own mutators (add/
remove/set_mode's targeted _invalidate_qot_sharing_oms, apply_nf_delta/
apply_loss_delta's full clear) are what's responsible for marking exactly
what went stale. Two correctness properties matter most: (1) it must not skip
work that's actually needed (validated by the disjoint-route-untouched case
below), and (2) it must never suppress the failed-asset sentinel re-derivation
(S8-1), which relies on running unconditionally every call regardless of
staleness.
"""
from multilayer_optical_mcp.gnpy_adapter import adapter as adapter_module
from multilayer_optical_mcp.model.assets import Lightpath
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading
from multilayer_optical_mcp.model.whatif import loading_from_model
from tests.gnpy_adapter.test_per_path_comb import _diamond_model, ROUTE1, ROUTE2, MODE


def _two_disjoint_lightpaths():
    model = _diamond_model()
    model.add_lightpath(Lightpath(id="lp1", oms_sequence=ROUTE1,
                                  mode_id=MODE, center_freq_hz=193.4e12))
    model.add_lightpath(Lightpath(id="lp2", oms_sequence=ROUTE2,
                                  mode_id=MODE, center_freq_hz=193.7e12))
    store = QoTResultStore()
    recompute_qot_under_loading(model=model, store=store,
                                loading=loading_from_model(model))
    return model, store


def _count_gated_qot_calls(monkeypatch):
    calls = []
    real = adapter_module.gated_qot

    def spy(**kwargs):
        calls.append(kwargs["oms_sequence"])
        return real(**kwargs)
    monkeypatch.setattr(adapter_module, "gated_qot", spy)
    return calls


def test_only_missing_skips_lightpaths_with_a_stored_qot_state(monkeypatch):
    model, store = _two_disjoint_lightpaths()
    # Simulate what _invalidate_qot_sharing_oms does after a real mutation
    # touching only lp1's neighborhood: clear ONLY lp1's entry.
    model._qot_state.pop("lp1", None)
    lp2_state_before = model.get_qot_state("lp2")

    calls = _count_gated_qot_calls(monkeypatch)
    recompute_qot_under_loading(model=model, store=store,
                                loading=loading_from_model(model),
                                only_missing=True)

    assert calls == [ROUTE1], (
        f"expected exactly one real GNPy call, for lp1's route only; got {calls}")
    assert model.get_qot_state("lp1") is not None  # recomputed
    assert model.get_qot_state("lp2") == lp2_state_before  # untouched, not just unchanged


def test_only_missing_false_still_recomputes_everything(monkeypatch):
    """Default behavior (every other existing caller) is unaffected: nothing
    is skipped regardless of what's already stored."""
    model, store = _two_disjoint_lightpaths()
    calls = _count_gated_qot_calls(monkeypatch)
    recompute_qot_under_loading(model=model, store=store,
                                loading=loading_from_model(model))
    assert sorted(calls) == sorted([ROUTE1, ROUTE2])


def test_only_missing_still_reapplies_failed_sentinel_over_a_stale_entry():
    """The highest-stakes correctness property: mark_failed never invalidates
    _qot_state (S8-1's own docstring: recompute re-derives the sentinel from
    failed_assets() every call, unconditionally). only_missing must not let a
    PRESENT-but-now-wrong real QoT value survive a mutation that later marks
    one of its own crossed assets failed."""
    model, store = _two_disjoint_lightpaths()
    feasible_lp1 = model.get_qot_state("lp1")
    assert feasible_lp1.margin_db > float("-inf")  # sanity: a real, finite value

    # lp1 crosses oms_A_M; fail one of its member fibers.
    fiber_id = next(iter(model.get_oms("oms_A_M").elements))
    model.mark_failed((fiber_id,))

    # lp1's entry is still the old, now-stale FEASIBLE value -- mark_failed
    # did not touch _qot_state. only_missing=True must still overwrite it.
    assert model.get_qot_state("lp1") == feasible_lp1

    recompute_qot_under_loading(model=model, store=store,
                                loading=loading_from_model(model),
                                only_missing=True)

    st = model.get_qot_state("lp1")
    assert st.margin_db == float("-inf"), (
        "only_missing must not suppress the failed-asset sentinel re-derivation "
        "for a lightpath whose stored QoT predates the new failure")
