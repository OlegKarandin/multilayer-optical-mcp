from multilayer_optical_mcp.model.assets import Lightpath, IPLink, Router
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.snapshots import SnapshotStore
from multilayer_optical_mcp.model.plan import Plan, ProvisionLightpath, TeardownLightpath
from multilayer_optical_mcp.model.commit import commit_plan, reconcile
from tests.phase7_topology import new_model, add_bidir_span


def _base():
    """Two synthesizable spans: direct A->B (oms1) and multi-hop A->C->B
    (omsAC, omsCB), no lightpaths yet. Avoids ambiguous parallel reverse OMS."""
    m = new_model()
    add_bidir_span(m, "A", "B", "oms1")
    add_bidir_span(m, "A", "C", "omsAC")
    add_bidir_span(m, "C", "B", "omsCB")
    m.add_router(Router(id="rA", site="A"))
    m.add_router(Router(id="rB", site="B"))
    return m


def _two_provisions():
    return Plan(ops=(
        ProvisionLightpath(
            lightpath=Lightpath(id="lpX", oms_sequence=("oms1",), mode_id="400G",
                                center_freq_hz=193.4e12),
            ip_link=IPLink(id="ipX", a_router="rA", z_router="rB", lightpath_id="lpX")),
        ProvisionLightpath(
            lightpath=Lightpath(id="lpY", oms_sequence=("omsAC", "omsCB"), mode_id="400G",
                                center_freq_hz=193.4e12),
            ip_link=IPLink(id="ipY", a_router="rA", z_router="rB", lightpath_id="lpY")),
    ))


def test_dry_run_does_not_touch_ground_truth():
    store = SnapshotStore(_base())
    results = QoTResultStore()
    result = commit_plan(store, _two_provisions(), store_results=results, dry_run=True)
    assert result.dry_run is True
    assert "lpX" not in store.current()._lightpaths   # ground truth untouched
    assert result.diff["lightpaths"]["added"] == ("lpX", "lpY")  # simulated delta


def test_live_commit_with_violations_is_rejected():
    store = SnapshotStore(_base())
    results = QoTResultStore()
    # tear down a lightpath that does not exist -> a plan error surfaces as rejected
    bad = Plan(ops=(TeardownLightpath(lightpath_id="nope"),))
    result = commit_plan(store, bad, store_results=results, dry_run=False, confirm=True)
    assert result.status == "rejected"
    assert "nope" not in store.current()._lightpaths


def test_live_commit_requires_confirm():
    store = SnapshotStore(_base())
    results = QoTResultStore()
    result = commit_plan(store, _two_provisions(), store_results=results,
                         dry_run=False, confirm=False)
    assert result.status == "requires_approval"
    assert "lpX" not in store.current()._lightpaths


def test_partial_commit_then_reconcile_surfaces_drift():
    store = SnapshotStore(_base())
    results = QoTResultStore()

    def flaky_actuator(model, op):
        # the second provision (lpY) "times out" at the control plane
        if isinstance(op, ProvisionLightpath) and op.lightpath.id == "lpY":
            return False
        from multilayer_optical_mcp.model.plan import apply_op
        apply_op(model, op)
        return True

    result = commit_plan(store, _two_provisions(), store_results=results,
                         dry_run=False, confirm=True, actuator=flaky_actuator)
    assert result.status == "committed_with_failures"
    assert result.failed_ops == 1
    assert "lpX" in store.current()._lightpaths       # first op actuated
    assert "lpY" not in store.current()._lightpaths   # second failed

    drift = reconcile(store, result.intended_snapshot_id)
    assert not drift.in_sync
    # intended has lpY/ipY that reality lacks -> drift names them
    drifted = {(d.registry, d.asset_id) for d in drift.drift}
    assert ("lightpaths", "lpY") in drifted
    assert ("ip_links", "ipY") in drifted


def test_reconcile_on_evicted_intended_snapshot_returns_drift_not_raise():
    """Regression for the audit's Important finding: reconcile() must not
    raise a bare KeyError when the intended snapshot was evicted."""
    from multilayer_optical_mcp.model.network import NetworkModel
    from multilayer_optical_mcp.model.modes import ModeRegistry
    from multilayer_optical_mcp.model.assets import TransceiverMode
    from multilayer_optical_mcp.model.snapshots import SnapshotStore
    from multilayer_optical_mcp.model.commit import reconcile

    base = NetworkModel(modes=ModeRegistry([TransceiverMode(
        id="400G", bitrate_gbps=400.0, required_gsnr_db=7.1,
        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)]))
    store = SnapshotStore(base, max_snapshots=1)
    sid = store.put(base.clone())
    store.put(base.clone())   # evicts sid (max_snapshots=1)

    report = reconcile(store, sid)   # must not raise
    assert report.in_sync is False
    assert report.drift and report.drift[0].asset_id == sid
