# tests/test_server_phase6.py
import json
import math
import pytest
from multilayer_optical_mcp.server import build_app, MOD_FORMATS_YAML
from multilayer_optical_mcp.model.assets import FiberType, Amplifier, Fiber, OMS, ROADM, Lightpath, Transceiver
from multilayer_optical_mcp.model.modes import load_modulation_formats
from multilayer_optical_mcp.model.qot import QoTState


def _call(app, name, **kwargs):
    """Invoke a registered FastMCP tool's underlying function directly."""
    return app._tool_manager._tools[name].fn(**kwargs)


def _assert_json_finite(obj):
    """Recursively assert no non-finite float survives a json.dumps/json.loads
    round-trip. Python's json.loads is lenient (it parses the bare Infinity/
    -Infinity/NaN tokens json.dumps emits by default), so equality after a
    round-trip alone wouldn't catch a leak -- walk the reloaded structure and
    require every float to be finite."""
    if isinstance(obj, float):
        assert math.isfinite(obj), f"non-finite float leaked through JSON: {obj!r}"
    elif isinstance(obj, dict):
        for v in obj.values():
            _assert_json_finite(v)
    elif isinstance(obj, list):
        for v in obj:
            _assert_json_finite(v)


def _seed_branch_with_lightpath(app):
    """Seed a branch with a simple one-hop network + one lightpath with QoT."""
    store = app._snapshots
    n = store.current()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="a1", type_variety="adv", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B", elements=("roadm_A", "a1", "fAB")))
    mode_id = n.modes.list()[0].id
    n.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",),
                              mode_id=mode_id, center_freq_hz=193.4e12))
    # Set QoT manually (no GNPy call needed for sweep test)
    n.set_qot_state("lpAB", QoTState(gsnr_db=10.0, osnr_db=22.0, margin_db=0.5))
    return mode_id


def test_whatif_sweep_tool_lists_fragile():
    app = build_app()
    _seed_branch_with_lightpath(app)
    out = _call(app, "whatif_margin_threshold_sweep", threshold_db=1.0)
    assert "fragile" in out
    assert any(row["lightpath_id"] == "lpAB" for row in out["fragile"])
    assert all(row["margin_db"] <= 1.0 for row in out["fragile"])


def test_whatif_sweep_tool_empty_when_all_healthy():
    app = build_app()
    _seed_branch_with_lightpath(app)
    # threshold below lpAB's margin of 0.5 => nothing fragile
    out = _call(app, "whatif_margin_threshold_sweep", threshold_db=0.1)
    assert "fragile" in out
    assert out["fragile"] == []


def test_inject_failure_tool_downs_lightpath():
    app = build_app()
    _seed_branch_with_lightpath(app)
    # fAB is in omsAB elements, so lpAB crosses it
    out = _call(app, "inject_failure", asset_ids=["fAB"])
    assert "downed_lightpaths" in out
    assert "lpAB" in out["downed_lightpaths"]
    assert "fAB" in out["failed_assets"]


def test_inject_failure_tool_unknown_asset_does_not_down_lightpath():
    app = build_app()
    _seed_branch_with_lightpath(app)
    out = _call(app, "inject_failure", asset_ids=["fXX"])
    assert out["downed_lightpaths"] == []
    assert "fXX" in out["failed_assets"]


def test_inject_failure_tool_multiple_assets():
    app = build_app()
    n = app._snapshots.current()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="a1", type_variety="adv", gain_db=20.0, nf_db=5.5))
    n.add_amplifier(Amplifier(id="a2", type_variety="adv", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fBC", a_end="a2", z_end="a3", length_km=80.0, type_variety="SSMF"))
    for node in ("A", "B", "C"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B", elements=("roadm_A", "a1", "fAB")))
    n.add_oms(OMS(id="omsBC", src_node_id="B", dst_node_id="C", elements=("roadm_B", "a2", "fBC")))
    mode_id = n.modes.list()[0].id
    n.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",),
                              mode_id=mode_id, center_freq_hz=193.4e12))
    n.add_lightpath(Lightpath(id="lpBC", oms_sequence=("omsBC",),
                              mode_id=mode_id, center_freq_hz=193.5e12))
    n.set_qot_state("lpAB", QoTState(gsnr_db=10.0, osnr_db=22.0, margin_db=2.0))
    n.set_qot_state("lpBC", QoTState(gsnr_db=10.0, osnr_db=22.0, margin_db=2.0))

    out = _call(app, "inject_failure", asset_ids=["fAB", "fBC"])
    assert set(out["downed_lightpaths"]) == {"lpAB", "lpBC"}
    assert set(out["failed_assets"]) == {"fAB", "fBC"}


# ---------------------------------------------------------------------------
# Task 13: non-finite floats (-inf QoT sentinel) sanitized to JSON-safe
# string tokens ("Infinity" / "-Infinity" / "NaN") at the serialization
# boundary in model/views.py, so json.dumps never emits the raw invalid
# Infinity/-Infinity/NaN tokens RFC 8259 forbids.
# ---------------------------------------------------------------------------


def test_get_lightpaths_sanitizes_failed_asset_margin():
    app = build_app()
    _seed_branch_with_lightpath(app)
    # inject_failure is physics-free: it writes the real -inf QoT sentinel
    # directly (see whatif.inject_failure), no GNPy call or mocking involved.
    _call(app, "inject_failure", asset_ids=["fAB"])

    out = _call(app, "get_lightpaths")
    lp = next(r for r in out if r["id"] == "lpAB")
    assert lp["qot"]["margin_db"] == "-Infinity"
    assert lp["qot"]["gsnr_db"] == "-Infinity"
    assert lp["qot"]["osnr_db"] == "-Infinity"

    payload = json.dumps(out)
    _assert_json_finite(json.loads(payload))


def test_whatif_margin_threshold_sweep_sanitizes_failed_asset_margin():
    app = build_app()
    _seed_branch_with_lightpath(app)
    _call(app, "inject_failure", asset_ids=["fAB"])

    out = _call(app, "whatif_margin_threshold_sweep", threshold_db=0.0)
    row = next(r for r in out["fragile"] if r["lightpath_id"] == "lpAB")
    assert row["margin_db"] == "-Infinity"
    assert row["gsnr_db"] == "-Infinity"

    payload = json.dumps(out)
    _assert_json_finite(json.loads(payload))


def _seed_gnpy_app_with_two_amp_lightpath():
    """A real, GNPy-propagatable one-lightpath network built via the importer
    (model_from_abstract_graph gives every OMS a paired reverse leg, which
    gated_qot's backward propagation requires -- see the C2 reverse-OMS memory
    note), so inject_degradation's internal recompute_qot_under_loading runs
    actual physics rather than reading a manually-set QoTState. Mirrors
    tests/model/test_whatif.py's _one_edge_model + _live_model_one_lightpath."""
    from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    base = model_from_abstract_graph({
        "nodes": [{"id": 0}, {"id": 1}],
        "edges": [{"src": 0, "dst": 1, "length_km": 160.0, "num_spans": 2,
                   "span_lengths_km": [80.0, 80.0], "fiber_type": "SSMF",
                   "amplifier_nf_db": [5.5, 5.5]}],
    }, modes=modes)
    mode_id = modes.list()[0].id
    base.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_0_1",),
                                 mode_id=mode_id, center_freq_hz=193.4e12))
    return build_app(model=base), mode_id


def test_inject_degradation_sanitizes_nonfinite_margin_before():
    app, mode_id = _seed_gnpy_app_with_two_amp_lightpath()
    loading_channels = [{"center_freq_hz": 193.4e12, "slot_width_hz": 100e9,
                         "power_dbm": None, "mode_id": mode_id}]
    # Seed a real, finite baseline via an actual GNPy recompute.
    _call(app, "recompute_qot_under_loading", loading_channels=loading_channels)
    assert math.isfinite(app._snapshots.current().get_qot_state("lp0").margin_db)

    # inject_failure writes the real -inf QoT sentinel for lp0 (it crosses
    # fiber_0_1_0, on oms_0_1) via production code -- not a mocked value.
    _call(app, "inject_failure", asset_ids=["fiber_0_1_0"])
    assert math.isinf(app._snapshots.current().get_qot_state("lp0").margin_db)

    # inject_degradation's internal recompute must not resurrect a failed
    # lightpath (see test_failure_and_degradation_compose in
    # tests/model/test_whatif.py), so both margin_before and margin_after
    # come back as the -inf sentinel here -- genuinely produced, not mocked.
    # Degrade the OTHER amp (amp_0_1_1) so the perturbation itself isn't what
    # causes the non-finite value; the failed sentinel is.
    out = _call(app, "inject_degradation", asset_id="amp_0_1_1", nf_delta=1.0)
    row = next(r for r in out["rows"] if r["lightpath_id"] == "lp0")
    assert row["margin_before"] == "-Infinity"
    assert row["margin_after"] == "-Infinity"

    payload = json.dumps(out)
    _assert_json_finite(json.loads(payload))


def test_whatif_sensitivity_tool_flags_perturbed_amp():
    app = build_app()
    n = app._snapshots.current()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_roadm(ROADM(id="roadm_A"))
    n.add_roadm(ROADM(id="roadm_B"))
    n.add_transceiver(Transceiver(id="trx_A", site="A"))
    n.add_transceiver(Transceiver(id="trx_B", site="B"))
    n.add_amplifier(Amplifier(id="a1", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="roadm_A", z_end="a1", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="a2", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f2", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "a1", "f1", "a2", "f2")))
    mode_id = n.modes.list()[0].id

    id_a = _call(app, "snapshot_create")["id"]
    _call(app, "snapshot_branch", parent_id=id_a)   # move current() onto a working branch
    app._snapshots.current().apply_nf_delta("a2", 6.0)   # mutate the branch's live working copy
    # Task 12 fix: branch() now clones independently before storing, so the id
    # returned by snapshot_branch captures the PRE-mutation branch point, not
    # whatever current() is later mutated into. Capture the mutated state with
    # a fresh snapshot_create() instead of relying on the branch id for it.
    id_b = _call(app, "snapshot_create")["id"]

    loading_channels = [{"center_freq_hz": 193.4e12, "slot_width_hz": 100e9,
                         "power_dbm": None, "mode_id": mode_id}]
    out = _call(app, "whatif_sensitivity", state_a=id_a, state_b=id_b,
               oms_sequence=["omsAB"], direction="forward", mode_id=mode_id,
               loading_channels=loading_channels)
    assert out["delta_margin_db"] < 0
    assert out["rows"][0]["element_id"] == "a2"
    other = next(r for r in out["rows"] if r["element_id"] == "a1")
    assert abs(other["gsnr_contribution_delta_db"]) < 0.05


def test_compute_qot_tool_rejects_clashing_loading_channels():
    # Regression: LoadingState.union()'s clash check must be reachable from a
    # real production call path (the compute_qot MCP tool), not just from its
    # own unit test. compute_qot's loading_channels is meant to propagate along
    # ONE physical path, so two carriers at the same frequency there is a
    # genuine bug -- unlike recompute_qot_under_loading's network-wide comb,
    # which legitimately tolerates the same frequency reused on disjoint fibers
    # (see whatif.loading_from_model's docstring), that tool is untouched.
    app = build_app()
    mode_id = _seed_branch_with_lightpath(app)
    loading_channels = [
        {"center_freq_hz": 193.4e12, "slot_width_hz": 100e9,
         "power_dbm": None, "mode_id": mode_id},
        {"center_freq_hz": 193.4e12, "slot_width_hz": 100e9,
         "power_dbm": None, "mode_id": mode_id},
    ]
    with pytest.raises(ValueError, match="spectrum clash"):
        _call(app, "compute_qot", oms_sequence=["omsAB"], direction="forward",
             mode_id=mode_id, loading_channels=loading_channels)


def test_recompute_qot_under_loading_tool_still_tolerates_shared_frequency():
    # Contrast case: recompute_qot_under_loading's loading_channels is a
    # network-wide comb, not a single path's -- two entries sharing a
    # frequency (as ordinary wavelength reuse on physically disjoint fibers
    # would produce) is not a clash, so this tool must NOT route through
    # LoadingState.union() the way compute_qot now does.
    from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph

    modes = load_modulation_formats(MOD_FORMATS_YAML)
    base = model_from_abstract_graph({
        "nodes": [{"id": 0}, {"id": 1}],
        "edges": [{"src": 0, "dst": 1, "length_km": 160.0, "num_spans": 2,
                   "span_lengths_km": [80.0, 80.0], "fiber_type": "SSMF",
                   "amplifier_nf_db": [5.5, 5.5]}],
    }, modes=modes)
    mode_id = modes.list()[0].id
    base.add_lightpath(Lightpath(id="lpAB", oms_sequence=("oms_0_1",),
                                 mode_id=mode_id, center_freq_hz=193.4e12))
    app = build_app(model=base)

    loading_channels = [
        {"center_freq_hz": 193.4e12, "slot_width_hz": 100e9,
         "power_dbm": None, "mode_id": mode_id},
        {"center_freq_hz": 193.4e12, "slot_width_hz": 100e9,
         "power_dbm": None, "mode_id": mode_id},
    ]
    out = _call(app, "recompute_qot_under_loading", loading_channels=loading_channels)
    assert "lpAB" in out


# ---------------------------------------------------------------------------
# Final-review Finding 2: 3 more non-finite-float JSON leak sites Task 13's
# scope didn't cover -- recompute_qot_under_loading's own tool body,
# validation_report_dict's per-violation `detail` dict, and
# objective_result_dict's total_margin/scalar. Same technique as Task 13's
# own tests: a real inject_failure write (production code, not mocked)
# produces the -inf sentinel; each site must sanitize it.
# ---------------------------------------------------------------------------


def test_recompute_qot_under_loading_tool_sanitizes_failed_asset_margin():
    app = build_app()
    mode_id = _seed_branch_with_lightpath(app)
    # inject_failure writes the real -inf QoT sentinel (whatif.inject_failure);
    # recompute_qot_under_loading's own S8-1 logic (adapter.py) re-applies that
    # sentinel rather than resurrecting the lightpath with a feasible GSNR.
    _call(app, "inject_failure", asset_ids=["fAB"])

    loading_channels = [{"center_freq_hz": 193.4e12, "slot_width_hz": 100e9,
                         "power_dbm": None, "mode_id": mode_id}]
    out = _call(app, "recompute_qot_under_loading", loading_channels=loading_channels)
    row = out["lpAB"]
    assert row["margin_db"] == "-Infinity"
    assert row["gsnr_db"] == "-Infinity"
    assert row["osnr_db"] == "-Infinity"

    payload = json.dumps(out)
    _assert_json_finite(json.loads(payload))


def test_validate_plan_tool_sanitizes_mode_infeasible_detail_floats():
    app = build_app()
    _seed_branch_with_lightpath(app)
    # inject_failure writes the real -inf QoT sentinel for lpAB (crosses fAB).
    _call(app, "inject_failure", asset_ids=["fAB"])

    # Empty-ops plan validates the standing state (validate.py: `if not
    # plan.ops`), which is enough to surface lpAB's MODE_INFEASIBLE finding
    # (margin_db < 0) without needing any actual plan mutation.
    out = _call(app, "validate_plan", plan={"ops": []})
    assert out["ok"] is False
    v = next(v for v in out["violations"] if v["type"] == "mode_infeasible")
    # Discriminated-union flattening: MODE_INFEASIBLE's fields sit at the top
    # level of the violation dict now, not nested under "detail".
    assert v["margin_db"] == "-Infinity"
    assert v["gsnr_db"] == "-Infinity"
    # deficit_db = required_gsnr_db - gsnr_db = required - (-inf) = +inf.
    assert v["deficit_db"] == "Infinity"
    # Non-float values in the same violation must pass through unchanged.
    assert isinstance(v["feasible_downshift_modes"], list)

    payload = json.dumps(out)
    _assert_json_finite(json.loads(payload))


def test_evaluate_objective_tool_sanitizes_total_margin_and_scalar():
    app = build_app()
    _seed_branch_with_lightpath(app)
    # inject_failure writes the real -inf QoT sentinel for lpAB; evaluate_objective's
    # total_margin sums every lightpath's margin_db, so -inf propagates straight
    # into total_margin, and from there into the weighted scalar.
    _call(app, "inject_failure", asset_ids=["fAB"])

    out = _call(app, "evaluate_objective")
    assert out["total_margin"] == "-Infinity"
    assert out["scalar"] == "Infinity"   # scalar subtracts total_margin (- -inf = +inf)

    payload = json.dumps(out)
    _assert_json_finite(json.loads(payload))
