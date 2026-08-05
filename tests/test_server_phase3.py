"""Tests for Phase 3 server tools: read surface + risk groups.

Uses the same internal API as test_server.py:
  app._tool_manager._tools[name].fn(**kwargs)
"""
from __future__ import annotations

import asyncio

from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, SRLG
from multilayer_optical_mcp.model.ip_assets import Router, IPLink, Service


def _tool_names(app) -> set[str]:
    return set(app._tool_manager._tools.keys())


def _call(app, name: str, **kwargs):
    tool = app._tool_manager._tools[name]
    result = tool.fn(**kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _seed_app():
    """Build an app and populate its current snapshot with a 2-path scenario."""
    app = build_app()
    model = app._snapshots.current()
    model.register_fiber_type(FiberType("SSMF", 0.2))
    for amp_id in ("ampA1", "ampA2", "ampB1", "ampB2"):
        model.add_amplifier(Amplifier(id=amp_id, type_variety="advanced_toy",
                                      gain_db=20.0, nf_db=5.5))
    model.add_fiber(Fiber("fiber-north", "ampA1", "ampA2", 80.0, "SSMF"))
    model.add_fiber(Fiber("fiber-south", "ampB1", "ampB2", 80.0, "SSMF"))
    for node in ("A", "B"):
        model.add_roadm(ROADM(id=f"roadm_{node}"))
    model.add_oms(OMS("oms-north", "A", "B", ("roadm_A", "ampA1", "fiber-north", "ampA2")))
    model.add_oms(OMS("oms-south", "A", "B", ("roadm_A", "ampB1", "fiber-south", "ampB2")))
    model.add_lightpath(Lightpath("lp-north", ("oms-north",), "400G@7.1dB", 193.4e12))
    model.add_lightpath(Lightpath("lp-south", ("oms-south",), "400G@7.1dB", 193.4e12))
    model.add_router(Router("R1", "A"))
    model.add_router(Router("R2", "B"))
    model.add_ip_link(IPLink("ip1", "R1", "R2", "lp-north"))
    model.add_ip_link(IPLink("ip2", "R1", "R2", "lp-south"))
    model.add_service(Service("svc1", "R1", "R2", 10.0,
                              working_path=("ip1",), protection_path=("ip2",)))
    model.add_srlg(SRLG("srlg-pole-A", ("fiber-north",)))
    return app


def test_server_registers_phase_3_tools():
    app = build_app()
    names = _tool_names(app)
    expected = {
        "get_topology", "get_lightpaths", "get_services", "get_traffic_matrix",
        "list_srlgs", "get_srlg_members",
        "define_risk_group", "list_risk_groups", "get_risk_group",
        "get_exposure",
    }
    assert expected.issubset(names)


def test_get_topology_layer_optical_excludes_ip():
    app = _seed_app()
    out = _call(app, "get_topology", layer="optical")
    assert "fibers" in out and "ip_links" not in out


def test_get_lightpaths_and_get_services():
    app = _seed_app()
    lps = _call(app, "get_lightpaths")
    assert {lp["id"] for lp in lps} == {"lp-north", "lp-south"}
    svcs = _call(app, "get_services")
    assert svcs["services"][0]["id"] == "svc1"
    assert svcs["grooming_map"]["lp-north"] == ["svc1"]


def test_get_traffic_matrix():
    app = _seed_app()
    tm = _call(app, "get_traffic_matrix")
    assert tm["R1"]["R2"] == 10.0


def test_list_srlgs_and_get_srlg_members():
    app = _seed_app()
    s = _call(app, "list_srlgs")
    assert s[0]["id"] == "srlg-pole-A"
    members = _call(app, "get_srlg_members", srlg_id="srlg-pole-A")
    assert members == ["fiber-north"]


def test_define_and_get_risk_group():
    app = _seed_app()
    out = _call(app, "define_risk_group",
                rg_id="rg-storm",
                asset_ids=["fiber-north", "fiber-south"],
                metadata={"source": "operator"})
    assert out["id"] == "rg-storm"
    fetched = _call(app, "get_risk_group", rg_id="rg-storm")
    assert sorted(fetched["asset_ids"]) == ["fiber-north", "fiber-south"]
    assert fetched["metadata"]["source"] == "operator"


def test_get_exposure_both_paths_intersect_after_storm_injection():
    """The headline scenario: design-time-disjoint pair, freshly-injected
    risk group spans both, exposure flags it."""
    app = _seed_app()
    _call(app, "define_risk_group",
          rg_id="rg-storm-cone",
          asset_ids=["fiber-north", "fiber-south"],
          metadata={})
    exp = _call(app, "get_exposure",
                service_id="svc1", risk_group_id="rg-storm-cone")
    assert exp["both_intersect"] is True
    assert exp["working_intersects"] is True
    assert exp["protection_intersects"] is True
