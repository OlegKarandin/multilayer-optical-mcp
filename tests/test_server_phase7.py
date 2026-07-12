import pytest

from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import FiberType
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
