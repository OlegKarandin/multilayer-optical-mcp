# tests/test_server_phase6.py
import math
import pytest
from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, ROADM, Lightpath, IPLink, Router, Service,
    Transceiver,
)
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore


def _call(app, name, **kwargs):
    """Invoke a registered FastMCP tool's underlying function directly."""
    return app._tool_manager._tools[name].fn(**kwargs)


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
    id_b = _call(app, "snapshot_branch", parent_id=id_a)["id"]
    app._snapshots.current().apply_nf_delta("a2", 6.0)   # mutate branch b only

    loading_channels = [{"center_freq_hz": 193.4e12, "slot_width_hz": 100e9,
                         "power_dbm": None, "mode_id": mode_id}]
    out = _call(app, "whatif_sensitivity", state_a=id_a, state_b=id_b,
               oms_sequence=["omsAB"], direction="forward", mode_id=mode_id,
               loading_channels=loading_channels)
    assert out["delta_margin_db"] < 0
    assert out["rows"][0]["element_id"] == "a2"
    other = next(r for r in out["rows"] if r["element_id"] == "a1")
    assert abs(other["gsnr_contribution_delta_db"]) < 0.05
