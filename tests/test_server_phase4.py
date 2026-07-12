"""Phase 4 server tools: routing + disjointness solvers, end-to-end through
the FastMCP app. Mirrors test_server_phase3.py."""
from __future__ import annotations

import asyncio

from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, Router, IPLink, Service, SRLG,
)


def _tool_names(app) -> set[str]:
    return set(app._tool_manager._tools.keys())


def _call(app, name: str, **kwargs):
    tool = app._tool_manager._tools[name]
    result = tool.fn(**kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _seed_app():
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
    return app


def test_server_registers_phase_4_tools():
    names = _tool_names(build_app())
    assert {"compute_paths", "check_disjointness",
            "compute_disjoint_paths"}.issubset(names)


def test_compute_paths_tool_returns_both_routes():
    app = _seed_app()
    out = _call(app, "compute_paths", src="A", dst="B", k=2)
    assert out["status"] == "solution"
    found = {tuple(p["oms_sequence"]) for p in out["paths"]}
    assert found == {("oms-north",), ("oms-south",)}


def test_compute_paths_tool_no_solution():
    app = _seed_app()
    out = _call(app, "compute_paths", src="A", dst="nowhere", k=2)
    assert out["status"] == "no_solution"
    assert out["paths"] == []


def test_check_disjointness_tool_risk_group_catch():
    """Same pair, physically disjoint but caught under a freshly-injected
    risk group spanning both spans."""
    app = _seed_app()
    _call(app, "define_risk_group", rg_id="rg-storm",
          asset_ids=["fiber-north", "fiber-south"], metadata={})
    phys = _call(app, "check_disjointness", path_a=["oms-north"],
                 path_b=["oms-south"], basis="physical", level="link")
    assert phys["disjoint"] is True
    rg = _call(app, "check_disjointness", path_a=["oms-north"],
               path_b=["oms-south"], basis="risk_group", level="risk_group")
    assert rg["disjoint"] is False
    assert rg["shared_groups"] == ["rg-storm"]


def test_compute_disjoint_paths_tool_solution_and_best_effort():
    app = _seed_app()
    sol = _call(app, "compute_disjoint_paths", src="A", dst="B",
                basis="physical", level="link", best_effort=False)
    assert sol["status"] == "solution"
    assert sol["disjoint"] is True

    _call(app, "define_risk_group", rg_id="rg-storm",
          asset_ids=["fiber-north", "fiber-south"], metadata={})
    none = _call(app, "compute_disjoint_paths", src="A", dst="B",
                 basis="risk_group", level="risk_group", best_effort=False)
    assert none["status"] == "no_solution"
    partial = _call(app, "compute_disjoint_paths", src="A", dst="B",
                    basis="risk_group", level="risk_group", best_effort=True)
    assert partial["status"] == "partial"
    assert partial["shared_groups"] == ["rg-storm"]
