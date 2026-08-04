import pytest

from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import FiberType, Router, Service
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


# ---------------------------------------------------------------------------
# followup-task-B: teardown_lightpath blast-radius + reroute_service warnings
# ---------------------------------------------------------------------------

def test_teardown_lightpath_reports_affected_services_before_mutation():
    """A real service riding the lightpath's IP link must be reported --
    with demand_gbps and the correct leg -- and the report must reflect
    state BEFORE the teardown (the lightpath is gone afterward)."""
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    n.add_service(Service(id="svc1", src_router="rA", dst_router="rB",
                          demand_gbps=123.0, working_path=("ip1",)))

    out = _call(app, "teardown_lightpath", lightpath_id="lp1")

    assert out["torn_down"] == "lp1"
    assert out["affected_services"] == [
        {"service_id": "svc1", "demand_gbps": 123.0, "legs": ["working"]},
    ]
    # And the teardown actually proceeded.
    assert "lp1" not in app._snapshots.current()._lightpaths


def test_teardown_lightpath_reports_empty_when_nothing_rides_it():
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})

    out = _call(app, "teardown_lightpath", lightpath_id="lp1")

    assert out["torn_down"] == "lp1"
    assert out["affected_services"] == []


def test_reroute_service_tool_warns_on_ip_overload():
    """A 500 Gbps demand rerouted onto a link bound to a 300G-capacity
    lightpath (least-demanding mode, 4.8 dB threshold) must trip a
    specific, actionable IP-overload warning naming the link, the offered
    load, and the derived capacity."""
    app = build_app()
    n = _seed(app)
    lo_mode = n.modes.list()[0].id   # 300G@4.8dB
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": lo_mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    n.set_qot_state("lp1", QoTState(gsnr_db=20.0, osnr_db=30.0, margin_db=5.0))
    n.add_service(Service(id="svcA", src_router="rA", dst_router="rB", demand_gbps=500.0))

    out = _call(app, "reroute_service", service_id="svcA", ip_path=["ip1"], which="working")

    assert out["working_path"] == ["ip1"]
    assert len(out["warnings"]) == 1
    warning = out["warnings"][0]
    assert "ip1" in warning
    assert "500" in warning     # offered load
    assert "300" in warning     # derived capacity


def test_reroute_service_tool_warns_on_disjointness_collapse():
    """Rerouting working onto the same OMS the protection leg already rides
    is exactly CLAUDE.md's scenario-1 catch: a design-time-disjoint pair
    that becomes correlated. The tool must surface it automatically,
    naming the shared OMS."""
    app = build_app()
    n = _seed(app)                       # rA(site A) <-> rB(site B) via omsAB
    add_bidir_span(n, "A", "C", "omsAC")
    add_bidir_span(n, "C", "B", "omsCB")
    n.add_router(Router(id="rC", site="C"))
    mode = n.modes.list()[0].id
    for lp_id, oms, a_router, z_router in [
        ("lpDirect", "omsAB", "rA", "rB"),
        ("lpAC", "omsAC", "rA", "rC"),
        ("lpCB", "omsCB", "rC", "rB"),
    ]:
        _call(app, "provision_lightpath",
              lightpath={"id": lp_id, "oms_sequence": [oms], "mode_id": mode,
                         "center_freq_hz": 193.4e12},
              ip_link={"id": f"ip_{lp_id}", "a_router": a_router, "z_router": z_router})
        n.set_qot_state(lp_id, QoTState(gsnr_db=20.0, osnr_db=30.0, margin_db=5.0))

    # Working direct (omsAB), protection via C (omsAC + omsCB) -- disjoint at setup.
    n.add_service(Service(id="svcP", src_router="rA", dst_router="rB", demand_gbps=50.0,
                          working_path=("ip_lpDirect",),
                          protection_path=("ip_lpAC", "ip_lpCB")))

    out = _call(app, "reroute_service", service_id="svcP",
                ip_path=["ip_lpAC", "ip_lpCB"], which="working")

    assert out["working_path"] == ["ip_lpAC", "ip_lpCB"]
    collapse_warnings = [w for w in out["warnings"] if "disjoint" in w.lower()]
    assert len(collapse_warnings) == 1
    assert "omsAC" in collapse_warnings[0]
    assert "omsCB" in collapse_warnings[0]


def test_reroute_service_tool_no_disjointness_warning_with_single_leg():
    """No protection_path yet -> the disjointness re-check must not even
    run (nothing to compare against), so warnings stays empty."""
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    n.set_qot_state("lp1", QoTState(gsnr_db=20.0, osnr_db=30.0, margin_db=5.0))
    n.add_service(Service(id="svcS", src_router="rA", dst_router="rB", demand_gbps=50.0))

    out = _call(app, "reroute_service", service_id="svcS", ip_path=["ip1"], which="working")

    assert out["working_path"] == ["ip1"]
    assert out["warnings"] == []


def test_reroute_service_tool_no_disjointness_warning_when_still_disjoint():
    """Both legs set, genuinely disjoint (no shared OMS) -- the re-check
    must run and find nothing to report."""
    app = build_app()
    n = _seed(app)
    add_bidir_span(n, "A", "C", "omsAC")
    add_bidir_span(n, "C", "B", "omsCB")
    n.add_router(Router(id="rC", site="C"))
    mode = n.modes.list()[0].id
    for lp_id, oms, a_router, z_router in [
        ("lpDirect", "omsAB", "rA", "rB"),
        ("lpAC", "omsAC", "rA", "rC"),
        ("lpCB", "omsCB", "rC", "rB"),
    ]:
        _call(app, "provision_lightpath",
              lightpath={"id": lp_id, "oms_sequence": [oms], "mode_id": mode,
                         "center_freq_hz": 193.4e12},
              ip_link={"id": f"ip_{lp_id}", "a_router": a_router, "z_router": z_router})
        n.set_qot_state(lp_id, QoTState(gsnr_db=20.0, osnr_db=30.0, margin_db=5.0))

    n.add_service(Service(id="svcQ", src_router="rA", dst_router="rB", demand_gbps=50.0,
                          working_path=("ip_lpDirect",)))

    out = _call(app, "reroute_service", service_id="svcQ",
                ip_path=["ip_lpAC", "ip_lpCB"], which="protection")

    assert out["protection_path"] == ["ip_lpAC", "ip_lpCB"]
    assert out["warnings"] == []


def test_reroute_service_tool_warns_on_protection_reservation_oversubscription():
    """A protection-leg reroute must be checked against working load PLUS the
    reservation it itself places (offered_load_per_link + reserved_capacity_per_link),
    not plain offered_load_per_link alone -- offered_load_per_link only ever
    sums working_path demand, so it is structurally blind to protection
    reservations (including the one this very reroute creates).

    ip1 is bound to lo_mode (300G@4.8dB). svcA's working_path already offers
    200 Gbps on ip1 -- on its own, well under capacity, so the plain
    offered_load_per_link check (used pre-fix, and still used for
    which="working") would NOT have fired. svcB's protection leg is then
    rerouted onto ip1 with a 150 Gbps demand: working(200) + reserved(150) =
    350 > 300 capacity. This is exactly the PROTECTION_OVERSUBSCRIBED failure
    mode validate.py's _protection_oversubscription_findings catches, now
    surfaced automatically by reroute_service(which="protection") too."""
    app = build_app()
    n = _seed(app)
    lo_mode = n.modes.list()[0].id   # 300G@4.8dB
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": lo_mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    n.set_qot_state("lp1", QoTState(gsnr_db=20.0, osnr_db=30.0, margin_db=5.0))

    n.add_service(Service(id="svcA", src_router="rA", dst_router="rB", demand_gbps=200.0,
                          working_path=("ip1",)))
    n.add_service(Service(id="svcB", src_router="rA", dst_router="rB", demand_gbps=150.0))

    # Sanity: plain working-only load alone does not exceed capacity.
    assert _call(app, "reroute_service", service_id="svcA", ip_path=["ip1"],
                 which="working")["warnings"] == []

    out = _call(app, "reroute_service", service_id="svcB", ip_path=["ip1"],
                which="protection")

    assert out["protection_path"] == ["ip1"]
    assert len(out["warnings"]) == 1
    warning = out["warnings"][0]
    assert "ip1" in warning
    assert "protection" in warning.lower()
    assert "200" in warning     # working load
    assert "150" in warning     # reserved protection
    assert "350" in warning     # committed total
    assert "300" in warning     # derived capacity


def test_validate_plan_tool_has_output_schema_with_all_violation_types():
    """The actual point of the discriminated-union refactor: FastMCP must now
    publish a real outputSchema for validate_plan, naming every violation
    type's fields -- not the None it registered when the tool returned a bare
    dict. This is what makes the shape agent-visible before any call, the
    same way inputSchema already is."""
    app = build_app()
    tool = app._tool_manager._tools["validate_plan"]
    schema = tool.output_schema
    assert schema is not None
    # Pydantic's discriminated-union schema emits each variant as a named
    # entry under $defs (verified directly against this repo's installed
    # pydantic/mcp versions before writing this test) -- assert every
    # violation type's model name is discoverable in the generated schema.
    schema_text = str(schema)
    for expected in (
        "ModeInfeasibleViolation", "SpectrumClashViolation", "IpLinkOverloadViolation",
        "DroppedTrafficViolation", "DisjointnessCollapseViolation",
        "ProtectionNotViableViolation", "ProtectionOversubscribedViolation",
        "InvalidPlanViolation",
    ):
        assert expected in schema_text


def test_validate_plan_tool_output_schema_declares_safe_float_anyof():
    """Regression for the final-review finding: SafeFloat's PlainSerializer
    return_type only affects model_json_schema(mode="serialization"), NOT the
    validation-mode schema FastMCP actually publishes as tool.output_schema.
    Without the WithJsonSchema override, the published schema falsely claims
    every SafeFloat field (e.g. ModeInfeasibleViolation.margin_db) is a bare
    number, while the real wire value for a non-finite margin (e.g. a
    failed-asset -inf) is the JSON string "-Infinity". Assert the published
    schema now documents both shapes for a representative SafeFloat field."""
    app = build_app()
    tool = app._tool_manager._tools["validate_plan"]
    schema = tool.output_schema
    assert schema is not None
    defs = schema.get("$defs", {})
    mode_infeasible = defs["ModeInfeasibleViolation"]
    margin_schema = mode_infeasible["properties"]["margin_db"]
    assert "anyOf" in margin_schema
    types = {entry.get("type") for entry in margin_schema["anyOf"]}
    assert types == {"number", "string"}
    string_variant = next(e for e in margin_schema["anyOf"] if e.get("type") == "string")
    assert set(string_variant["enum"]) == {"Infinity", "-Infinity", "NaN"}


def test_commit_plan_tool_has_output_schema():
    """Same point as the validate_plan schema test, for commit_plan: it must
    also publish a real outputSchema now that its return annotation is
    CommitResultModel instead of bare dict."""
    app = build_app()
    tool = app._tool_manager._tools["commit_plan"]
    schema = tool.output_schema
    assert schema is not None
    assert "CommitResultModel" in str(schema)
