import pytest

from multilayer_optical_mcp.model.assets import Lightpath, IPLink
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.snapshots import SnapshotStore
from multilayer_optical_mcp.model.plan import Plan, ProvisionLightpath, apply_op
from multilayer_optical_mcp.model.commit import commit_plan
from multilayer_optical_mcp.model.validate import recompute_if_possible
from multilayer_optical_mcp.model.objective import evaluate_objective
from tests.phase7_topology import new_model, add_bidir_span


def _base():
    """One synthesizable A<->B span (oms1 + paired reverse oms1_rev)."""
    m = new_model()
    add_bidir_span(m, "A", "B", "oms1")
    return m


def _provision_one():
    return Plan(ops=(
        ProvisionLightpath(
            lightpath=Lightpath(id="lpX", oms_sequence=("oms1",), mode_id="400G",
                                center_freq_hz=193.4e12),
            ip_link=IPLink(id="ipX", a_router="rA", z_router="rB",
                           lightpath_id="lpX")),
    ))


def test_live_commit_seeds_qot_so_link_is_not_dark():
    """The §2 regression: after a confirmed live commit, the freshly-provisioned
    lightpath must have recorded QoT and its IP link must report derived (>0)
    capacity — not read dark via the LookupError path."""
    store = SnapshotStore(_base())
    results = QoTResultStore()
    result = commit_plan(store, _provision_one(), store_results=results,
                         dry_run=False, confirm=True)
    assert result.status == "committed"
    live = store.current()
    # Before the fix, both of these raise LookupError (no QoT seeded on commit).
    st = live.get_qot_state("lpX")
    assert st.margin_db >= 0                      # 400G over one 80km span is feasible
    assert live.ip_link_capacity_gbps("ipX") > 0  # capacity derived, link is lit


def test_committed_qot_matches_independent_recompute():
    """Post-commit QoT on the live model equals an independent recompute of the
    same end-state, and evaluate_objective counts that margin — the settled
    reality the scoring path predicts (within the predicted<->real comb gap)."""
    store = SnapshotStore(_base())
    results = QoTResultStore()
    plan = _provision_one()

    # Independent oracle: apply the same op to a clone of the pre-commit state and
    # recompute directly. Same computation the live commit should now perform.
    oracle = SnapshotStore(_base()).current().clone()
    for op in plan.ops:
        apply_op(oracle, op)
    recompute_if_possible(oracle, QoTResultStore())

    commit_plan(store, plan, store_results=results, dry_run=False, confirm=True)
    live = store.current()

    assert live.get_qot_state("lpX").margin_db == \
        oracle.get_qot_state("lpX").margin_db
    obj = evaluate_objective(live)
    assert obj.total_margin == live.get_qot_state("lpX").margin_db


def test_recompute_seam_can_be_disabled():
    """The recompute is an injectable seam: a no-op recompute reproduces the
    pre-fix behavior (link dark), proving the seam is honored and lets QoT-free
    tests opt out of driving GNPy."""
    store = SnapshotStore(_base())
    results = QoTResultStore()
    result = commit_plan(store, _provision_one(), store_results=results,
                         dry_run=False, confirm=True,
                         recompute=lambda m, s: None)
    assert result.status == "committed"
    with pytest.raises(LookupError):
        store.current().get_qot_state("lpX")
