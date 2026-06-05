"""Phase 4 (rest) server tools: check_spectrum_feasibility, solve_rsa,
solve_allocation, end-to-end through the FastMCP app over the default toy_2span
gnpy topology (real GNPy). Mirrors test_server_phase4.py."""
from __future__ import annotations

import asyncio

from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, ROADM, OMS, Lightpath,
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
    """Seed the current model with the single-route toy that matches the default
    gnpy topology (toy_2span.json), so the real adapter resolves the OMS path."""
    app = build_app()
    m = app._snapshots.current()
    m.register_fiber_type(FiberType("SSMF", 0.2))
    m.add_roadm(ROADM(id="ROADM A", target_pch_out_db=-20.0))
    for amp in ("booster A", "east edfa in ILA", "east edfa at Z"):
        m.add_amplifier(Amplifier(id=amp, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber("east fiber A to ILA", "ROADM A", "east edfa in ILA", 80.0, "SSMF"))
    m.add_fiber(Fiber("east fiber ILA to Z", "east edfa in ILA", "east edfa at Z", 80.0, "SSMF"))
    m.add_oms(OMS("oms-AZ", "trx A", "trx Z", (
        "ROADM A", "booster A", "east fiber A to ILA",
        "east edfa in ILA", "east fiber ILA to Z", "east edfa at Z",
    )))
    return app, m


def test_server_registers_phase_4_rsa_tools():
    names = _tool_names(build_app())
    assert {"check_spectrum_feasibility", "solve_rsa",
            "solve_allocation"}.issubset(names)


def test_check_spectrum_feasibility_clash_after_lightpath():
    app, m = _seed_app()
    # Free slot -> feasible.
    free = _call(app, "check_spectrum_feasibility",
                 path=["oms-AZ"], center_freq_hz=193.4e12)
    assert free["feasible"] is True and free["clashes"] == []
    # Light a channel at that freq -> clash.
    m.add_lightpath(Lightpath("lp1", ("oms-AZ",), "400G@7.1dB", 193.4e12))
    clash = _call(app, "check_spectrum_feasibility",
                  path=["oms-AZ"], center_freq_hz=193.4e12)
    assert clash["feasible"] is False
    assert clash["clashes"][0]["oms_id"] == "oms-AZ"


def test_solve_rsa_places_demand_with_real_gnpy_mode():
    app, _ = _seed_app()
    out = _call(app, "solve_rsa",
                demands=[{"id": "d1", "src": "trx A", "dst": "trx Z"}])
    assert out["status"] == "solution"
    p = out["placements"][0]
    assert p["working"]["oms_path"]["oms_sequence"] == ["oms-AZ"]
    assert p["working"]["gsnr_db"] >= 7.1  # clears at least the lowest mode


def test_solve_allocation_greenfield_with_inventory():
    app, _ = _seed_app()
    out = _call(app, "solve_allocation",
                demands=[{"id": "d1", "src": "trx A", "dst": "trx Z",
                          "demand_gbps": 100.0}],
                spare_inventory={"trx A": 1, "trx Z": 1})
    assert out["status"] == "solution"
    assert out["placements"][0]["demand_id"] == "d1"
    assert out["unplaced"] == []


def test_solve_allocation_no_inventory_is_no_solution():
    app, _ = _seed_app()
    out = _call(app, "solve_allocation",
                demands=[{"id": "d1", "src": "trx A", "dst": "trx Z",
                          "demand_gbps": 100.0}],
                spare_inventory={"trx A": 0, "trx Z": 0})
    assert out["status"] == "no_solution"
    assert out["unplaced"][0]["demand_id"] == "d1"
