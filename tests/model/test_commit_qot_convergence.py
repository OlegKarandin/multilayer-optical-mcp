import pytest

from multilayer_optical_mcp.model.assets import Lightpath
from multilayer_optical_mcp.model.ip_assets import IPLink, Router
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.snapshots import SnapshotStore
from multilayer_optical_mcp.model.plan import (
    Plan, ProvisionLightpath, SetModulationFormat, apply_op,
)
from multilayer_optical_mcp.model.commit import commit_plan
from multilayer_optical_mcp.model.validate import recompute_if_possible
from multilayer_optical_mcp.model.objective import evaluate_objective
from tests.phase7_topology import new_model, add_bidir_span


def _base():
    """One synthesizable A<->B span (oms1 + paired reverse oms1_rev)."""
    m = new_model()
    add_bidir_span(m, "A", "B", "oms1")
    m.add_router(Router(id="rA", site="A"))
    m.add_router(Router(id="rB", site="B"))
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


def test_dry_run_diff_includes_qot_delta():
    """§5 regression: dry_run must run the same post-op recompute the live path
    does (on its own throwaway clone), so the returned diff carries the QoT
    delta a real commit would actually produce -- not just the structural
    (lightpaths/ip_links) change."""
    store = SnapshotStore(_base())
    results = QoTResultStore()

    # Seed one live lightpath so there is a pre-existing GSNR to perturb.
    commit_plan(store, _provision_one(), store_results=results,
               dry_run=False, confirm=True)
    baseline = store.current().get_qot_state("lpX")

    # Dry-run a downshift 400G->200G. Mode determines the channel's baud rate,
    # which is part of the loading fed to GNPy (build_si_for_loading), so this
    # is a genuine loading change -- not a label swap -- and it moves GSNR: a
    # narrower channel sees less noise bandwidth (verified: ~19.4dB -> ~22.3dB
    # on this fixture's single 80km span).
    downshift = Plan(ops=(SetModulationFormat(lightpath_id="lpX", mode_id="200G"),))
    result = commit_plan(store, downshift, store_results=results, dry_run=True)

    assert result.status == "dry_run"
    # Ground truth is untouched by the dry run.
    assert store.current().get_qot_state("lpX") == baseline
    assert store.current()._lightpaths["lpX"].mode_id == "400G"

    # Independent oracle: apply the same op to a fresh clone and recompute
    # directly -- the computation the fix makes dry_run perform internally.
    oracle = store.current().clone()
    apply_op(oracle, SetModulationFormat(lightpath_id="lpX", mode_id="200G"))
    recompute_if_possible(oracle, QoTResultStore())
    oracle_gsnr = oracle.get_qot_state("lpX").gsnr_db
    assert oracle_gsnr != baseline.gsnr_db   # sanity: the op really moves GSNR

    # lpX's recorded QoT shifted ("modified") -- exactly the delta a real
    # commit would report, and what dry_run was silently dropping before the
    # fix (before, it would show up ONLY in "lightpaths", never "qot_state").
    qot_delta = result.diff["qot_state"]
    assert "lpX" in qot_delta["modified"]
    assert "lpX" in result.diff["lightpaths"]["modified"]


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
