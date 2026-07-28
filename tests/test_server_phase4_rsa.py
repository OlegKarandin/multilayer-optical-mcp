"""Phase 4 (rest) server tools: check_spectrum_feasibility, solve_rsa,
solve_allocation, end-to-end through the FastMCP app over the default toy_2span
gnpy topology (real GNPy). Mirrors test_server_phase4.py."""
from __future__ import annotations

import asyncio

from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, ROADM, OMS, Lightpath, Transceiver, Router,
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
    """Seed the current model with a bidirectional single-route toy synthesized
    by the adapter. Both directions are modeled as physically separate OMS
    (oms-AZ / oms-ZA) so the real adapter resolves forward AND backward QoT."""
    app = build_app()
    m = app._snapshots.current()
    m.register_fiber_type(FiberType("SSMF", 0.2))
    # S3-11 Option B: both ends terminate at a ROADM (roadm_<id>) with a
    # registered transceiver; OMS endpoints are node ids "A"/"Z".
    m.add_roadm(ROADM(id="roadm_A", target_pch_out_db=-20.0))
    m.add_roadm(ROADM(id="roadm_Z", target_pch_out_db=-20.0))
    m.add_transceiver(Transceiver(id="trx_A", site="A"))
    m.add_transceiver(Transceiver(id="trx_Z", site="Z"))
    # solve_allocation is now IP-aware (grooms onto lightpaths' bound IP links), so
    # it needs a router at each demand endpoint. solve_rsa ignores routers.
    m.add_router(Router(id="r_A", site="A"))
    m.add_router(Router(id="r_Z", site="Z"))
    for amp in ("booster A", "east edfa in ILA", "east edfa at Z",
                "booster Z", "west edfa in ILA", "west edfa at A"):
        m.add_amplifier(Amplifier(id=amp, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber("east fiber A to ILA", "roadm_A", "east edfa in ILA", 80.0, "SSMF"))
    m.add_fiber(Fiber("east fiber ILA to Z", "east edfa in ILA", "east edfa at Z", 80.0, "SSMF"))
    m.add_fiber(Fiber("west fiber Z to ILA", "roadm_Z", "west edfa in ILA", 80.0, "SSMF"))
    m.add_fiber(Fiber("west fiber ILA to A", "west edfa in ILA", "west edfa at A", 80.0, "SSMF"))
    m.add_oms(OMS("oms-AZ", "A", "Z", (
        "roadm_A", "booster A", "east fiber A to ILA",
        "east edfa in ILA", "east fiber ILA to Z", "east edfa at Z",
    )))
    m.add_oms(OMS("oms-ZA", "Z", "A", (
        "roadm_Z", "booster Z", "west fiber Z to ILA",
        "west edfa in ILA", "west fiber ILA to A", "west edfa at A",
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
                demands=[{"id": "d1", "src": "A", "dst": "Z"}])
    assert out["status"] == "solution"
    p = out["placements"][0]
    assert p["working"]["oms_path"]["oms_sequence"] == ["oms-AZ"]
    assert p["working"]["gsnr_db"] >= 7.1  # clears at least the lowest mode


def test_solve_rsa_shares_harvest_cache_across_tool_calls(monkeypatch):
    """§5 regression: solve_rsa used to build a fresh HarvestCache() inside the
    tool body, so it started cold on every call -- two back-to-back calls for
    the same demand over an unchanged model would each miss and re-harvest
    from scratch. build_app now constructs one HarvestCache and threads it
    through every call; a second identical solve_rsa call should find its
    probe already harvested."""
    from multilayer_optical_mcp.model.qot_results import HarvestCache

    app, _ = _seed_app()

    hits: list[bool] = []
    original_get = HarvestCache.get

    def spy_get(self, key):
        result = original_get(self, key)
        hits.append(result is not None)
        return result

    monkeypatch.setattr(HarvestCache, "get", spy_get)

    demand = {"id": "d1", "src": "A", "dst": "Z"}

    out1 = _call(app, "solve_rsa", demands=[demand])
    assert out1["status"] == "solution"
    assert hits and not any(hits)   # first call: cache starts empty, all misses

    hits.clear()
    out2 = _call(app, "solve_rsa", demands=[demand])
    assert out2["status"] == "solution"
    # Second call over the SAME unchanged model reuses the first call's
    # harvested comb -- proof the cache instance, not just its behavior,
    # is shared across tool invocations (a coincidentally-identical fresh
    # cache would show only misses here too).
    assert hits and any(hits)


def test_solve_allocation_greenfield_with_inventory():
    app, _ = _seed_app()
    out = _call(app, "solve_allocation",
                demands=[{"id": "d1", "src": "A", "dst": "Z",
                          "demand_gbps": 100.0}],
                spare_inventory={"A": 1, "Z": 1})
    assert out["status"] == "solution"
    assert out["placements"][0]["demand_id"] == "d1"
    assert out["unplaced"] == []


def test_solve_allocation_no_inventory_is_no_solution():
    app, _ = _seed_app()
    out = _call(app, "solve_allocation",
                demands=[{"id": "d1", "src": "A", "dst": "Z",
                          "demand_gbps": 100.0}],
                spare_inventory={"A": 0, "Z": 0})
    assert out["status"] == "no_solution"
    assert out["unplaced"][0]["demand_id"] == "d1"
