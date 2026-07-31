import pytest

from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import FiberType, Router
from multilayer_optical_mcp.model.qot import QoTState
from tests.phase7_topology import add_bidir_span


def _call(app, name, **kwargs):
    return app._tool_manager._tools[name].fn(**kwargs)


def _seed(app):
    """Register a synthesizable bidirectional span A<->B (fwd OMS 'omsAB') on the
    app's live model, so a validate/recompute path drives real GNPy."""
    n = app._snapshots.current()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    add_bidir_span(n, "A", "B", "omsAB")
    n.add_router(Router(id="rA", site="A"))
    n.add_router(Router(id="rB", site="B"))
    return n


def test_provision_tool_adds_lightpath_and_binds_link():
    app = build_app()
    n = _seed(app)
    out = _call(app, "provision_lightpath",
                lightpath={"id": "lp1", "oms_sequence": ["omsAB"],
                           "mode_id": n.modes.list()[0].id,
                           "center_freq_hz": 193.4e12},
                ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    assert out["lightpath_id"] == "lp1"
    assert out["ip_link_id"] == "ip1"
    assert "lp1" in app._snapshots.current()._lightpaths


def test_teardown_tool_removes_lightpath():
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    out = _call(app, "teardown_lightpath", lightpath_id="lp1")
    assert out["torn_down"] == "lp1"
    assert "lp1" not in app._snapshots.current()._lightpaths


def test_set_modulation_format_tool_changes_mode_and_capacity():
    app = build_app()
    n = _seed(app)
    modes = n.modes.list()
    hi, lo = modes[0].id, modes[-1].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": hi,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    app._snapshots.current().set_qot_state(
        "lp1", QoTState(gsnr_db=20.0, osnr_db=30.0, margin_db=5.0))
    out = _call(app, "set_modulation_format", lightpath_id="lp1", mode_id=lo)
    assert out["mode_id"] == lo
    assert app._snapshots.current().get_lightpath("lp1").mode_id == lo


def test_validate_plan_tool_returns_typed_report():
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    out = _call(app, "validate_plan", plan={"ops": []})
    assert "violations" in out and "ok" in out and "num_states" in out


def test_commit_dry_run_tool_reports_diff_without_mutating():
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    plan = {"ops": [
        {"op": "provision_lightpath",
         "lightpath": {"id": "lpX", "oms_sequence": ["omsAB"], "mode_id": mode,
                       "center_freq_hz": 193.4e12},
         "ip_link": {"id": "ipX", "a_router": "rA", "z_router": "rB"}}]}
    out = _call(app, "commit_plan", plan=plan, dry_run=True)
    assert out["status"] == "dry_run"
    assert out["diff"]["lightpaths"]["added"] == ["lpX"]
    assert "lpX" not in app._snapshots.current()._lightpaths


def test_commit_live_requires_confirm_then_reconcile_in_sync():
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    plan = {"ops": [
        {"op": "provision_lightpath",
         "lightpath": {"id": "lpX", "oms_sequence": ["omsAB"], "mode_id": mode,
                       "center_freq_hz": 193.4e12},
         "ip_link": {"id": "ipX", "a_router": "rA", "z_router": "rB"}}]}
    pending = _call(app, "commit_plan", plan=plan, dry_run=False, confirm=False)
    assert pending["status"] == "requires_approval"

    done = _call(app, "commit_plan", plan=plan, dry_run=False, confirm=True)
    assert done["status"] == "committed"
    assert "lpX" in app._snapshots.current()._lightpaths

    drift = _call(app, "reconcile", intended_snapshot_id=done["intended_snapshot_id"])
    assert drift["in_sync"] is True
    assert drift["drift"] == []


def test_validate_plan_tool_never_raises_on_bad_reference():
    """Regression for the audit's Critical finding: ProvisionLightpath's
    reference errors (unknown OMS/mode) must come back as a typed
    invalid_plan violation, not escape validate_plan raw."""
    app = build_app()
    _seed(app)
    bad_plan = {"ops": [{"op": "provision_lightpath",
                         "lightpath": {"id": "lpX", "oms_sequence": ["oms-ghost"],
                                       "mode_id": "no-such-mode",
                                       "center_freq_hz": 193.4e12}}]}
    out = _call(app, "validate_plan", plan=bad_plan)   # must not raise
    assert out["ok"] is False
    assert any(v["type"] == "invalid_plan" for v in out["violations"])


def test_validate_plan_tool_never_raises_on_malformed_json():
    """Regression for the audit's Critical finding: plan_from_dict's raw
    KeyError on a missing required key must not escape validate_plan."""
    app = build_app()
    _seed(app)
    malformed = {"ops": [{"op": "provision_lightpath",
                          "lightpath": {"id": "lpX"}}]}   # missing oms_sequence etc.
    out = _call(app, "validate_plan", plan=malformed)   # must not raise
    assert out["ok"] is False
    assert any(v["type"] == "invalid_plan" for v in out["violations"])


def test_commit_plan_tool_never_raises_on_malformed_json():
    """Regression for the audit's Critical finding: commit_plan has its OWN
    independent instance of the malformed-plan-JSON gap (a separate call
    site from validate_plan's)."""
    app = build_app()
    _seed(app)
    malformed = {"ops": [{"op": "provision_lightpath",
                          "lightpath": {"id": "lpX"}}]}
    out = _call(app, "commit_plan", plan=malformed, dry_run=True)   # must not raise
    assert out["status"] == "rejected"


def test_provision_lightpath_tool_seeds_qot_so_solvers_do_not_crash():
    """Regression for the audit's Critical finding: after a live
    provision_lightpath call, build_layered_graph/route_service/
    compute_restoration/solve_allocation must not raise LookupError."""
    from multilayer_optical_mcp.model.multilayer_graph import build_layered_graph
    app = build_app()
    _seed(app)
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"],
                     "mode_id": app._snapshots.current().modes.list()[0].id,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})

    build_layered_graph(app._snapshots.current())   # must not raise


# ---------------------------------------------------------------------------
# followup-task-A: spectrum clash guard + QoT recompute/diagnostics
# ---------------------------------------------------------------------------

def test_provision_lightpath_tool_rejects_spectrum_clash():
    """A second lightpath at the SAME center_freq_hz on the SAME OMS is a
    genuine physical impossibility (duplicate-frequency carrier). The tool
    must reject it with a typed error and must NOT mutate the model."""
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    before_ids = {lp.id for lp in app._snapshots.current().list_lightpaths()}

    out = _call(app, "provision_lightpath",
                lightpath={"id": "lp2", "oms_sequence": ["omsAB"], "mode_id": mode,
                           "center_freq_hz": 193.4e12})

    assert out["error"] == "spectrum_clash"
    assert out["oms_clashes"] == [{"oms_id": "omsAB", "lightpaths": ["lp1"]}]
    after = app._snapshots.current()
    assert {lp.id for lp in after.list_lightpaths()} == before_ids
    assert "lp2" not in after._lightpaths
    assert "ip1" in {l.id for l in after.list_ip_links()}   # untouched too


def test_provision_lightpath_tool_allows_non_clashing_frequency():
    """Regression guard: a different, on-grid frequency on the same OMS must
    still provision normally -- the clash check must not over-reject."""
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})

    out = _call(app, "provision_lightpath",
                lightpath={"id": "lp2", "oms_sequence": ["omsAB"], "mode_id": mode,
                           "center_freq_hz": 193.5e12})   # slot 21, not slot 20

    assert "error" not in out
    assert out["lightpath_id"] == "lp2"
    assert "lp2" in app._snapshots.current()._lightpaths


def test_provision_lightpath_tool_rejects_off_grid_frequency():
    """Fix for the review finding on _occupied_slot_error: a center_freq_hz
    whose slot falls outside SpectrumGrid.default()'s [0, num_slots) range
    (anchor 191.4e12 Hz, 100 GHz spacing, 48 slots -> valid up to ~196.1e12
    Hz) used to raise a raw, uncaught ValueError out of the tool. It must
    now come back as a typed `off_grid_frequency` error, and must NOT
    mutate the model -- same before/after discipline as the spectrum-clash
    rejection test above."""
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    before_ids = {lp.id for lp in app._snapshots.current().list_lightpaths()}

    out = _call(app, "provision_lightpath",
                lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                           "center_freq_hz": 210e12},   # far outside the 48-slot grid
                ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})

    assert out["error"] == "off_grid_frequency"
    assert out["center_freq_hz"] == 210e12
    assert "detail" in out
    after = app._snapshots.current()
    assert {lp.id for lp in after.list_lightpaths()} == before_ids
    assert "lp1" not in after._lightpaths
    assert "ip1" not in {l.id for l in after.list_ip_links()}


def test_set_modulation_format_tool_recomputes_qot():
    """Regression: set_modulation_format used to never re-seed QoT after a
    mode change, leaving ip_link_capacity_gbps reporting "unknown" (a
    LookupError) instead of a real recompute. Real GNPy-backed topology,
    no mocks."""
    app = build_app()
    n = _seed(app)
    modes = n.modes.list()
    hi, lo = modes[0].id, modes[-1].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": hi,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})

    out = _call(app, "set_modulation_format", lightpath_id="lp1", mode_id=lo)

    assert out["mode_id"] == lo
    assert isinstance(out["gsnr_db"], float)
    assert isinstance(out["osnr_db"], float)
    assert isinstance(out["margin_db"], float)
    assert out["mode_feasible"] is True     # ~18.3 dB GSNR on this span, comfortably above every mode
    assert out["warnings"] == []
    # And the model itself carries the freshly-recomputed state, not just the
    # tool's return dict (proves it was actually re-seeded, not fabricated).
    st = app._snapshots.current().get_qot_state("lp1")   # must not raise LookupError
    assert st.margin_db == out["margin_db"]


def test_mode_feasibility_warning_fires_on_negative_margin():
    """Construct a REAL negative-margin scenario (span loss pushed well past
    the least-demanding mode's threshold) and confirm the tool surfaces a
    specific, actionable warning naming the lightpath and the affected IP
    link -- not a generic message."""
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id   # 300G@4.8dB: the least-demanding mode
    n.apply_loss_delta("f_omsAB", 25.0)   # blows the ~18.3 dB nominal budget

    out = _call(app, "provision_lightpath",
                lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                           "center_freq_hz": 193.4e12},
                ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})

    assert out["mode_feasible"] is False
    assert out["margin_db"] < 0
    assert len(out["warnings"]) == 1
    warning = out["warnings"][0]
    assert "lp1" in warning
    assert "ip1" in warning
    assert "0" in warning   # names the resulting 0 Gbps derived capacity


def test_provision_lightpath_tool_warnings_empty_list_when_healthy():
    """`warnings` must be present as an empty list (not omitted) on a
    healthy provision -- a consistent shape for an agent to parse."""
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    out = _call(app, "provision_lightpath",
                lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                           "center_freq_hz": 193.4e12},
                ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})

    assert out["mode_feasible"] is True
    assert "warnings" in out
    assert out["warnings"] == []
