"""Behavioural tests for the layered-engine rebase of solve_allocation.

Two facts the flat greenfield packer could not express:
  1. greenfield (empty net) still packs -> new-lightpath-only placements;
  2. under transponder scarcity a demand GROOMS onto a surviving lightpath's
     residual capacity instead of returning a false no_solution.
"""
from multilayer_optical_mcp.model.assets import (
    ROADM, FiberType, Fiber, Amplifier, OMS, TransceiverMode, Router, IPLink,
    Lightpath,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.spectrum import SpectrumGrid
from multilayer_optical_mcp.model.allocation import solve_allocation


class FakeQot:
    def __init__(self, gsnr_by_route):
        self._g = {tuple(k): v for k, v in gsnr_by_route.items()}

    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        return QoTState(gsnr_db=self._g[tuple(oms_sequence)], osnr_db=30.0, margin_db=0.0)


def _modes() -> ModeRegistry:
    return ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=5.0,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
        TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=15.0,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    ])


def _two_routes_with_routers() -> NetworkModel:
    n = NetworkModel(modes=_modes())
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("aN1", "aN2", "aS1", "aS2"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber("fN", "aN1", "aN2", 80.0, "SSMF"))
    n.add_fiber(Fiber("fS", "aS1", "aS2", 120.0, "SSMF"))
    for node in ("A", "Z"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS("oms-north", "A", "Z", ("roadm_A", "aN1", "fN", "aN2")))
    n.add_oms(OMS("oms-south", "A", "Z", ("roadm_A", "aS1", "fS", "aS2")))
    # routers colocated with optical nodes A/Z (Router.site == optical-node id)
    n.add_router(Router(id="r_A", site="A"))
    n.add_router(Router(id="r_Z", site="Z"))
    return n


def _hi_qot() -> FakeQot:
    return FakeQot({("oms-north",): 16.0, ("oms-south",): 16.0})


def _model_with_survivor_lightpath() -> NetworkModel:
    """A lit lightpath A->Z (400G, margin>=0) with residual capacity and no load."""
    n = _two_routes_with_routers()
    grid = SpectrumGrid.default()
    n.add_lightpath(Lightpath(id="lp-survivor", oms_sequence=("oms-north",),
                              mode_id="400G", center_freq_hz=grid.freq(0)))
    n.add_ip_link(IPLink(id="ipl-survivor", a_router="r_A", z_router="r_Z",
                         lightpath_id="lp-survivor"))
    req = n.modes.get("400G").required_gsnr_db
    n.set_qot_state("lp-survivor",
                    QoTState(gsnr_db=16.0, osnr_db=30.0, margin_db=16.0 - req))
    return n


def test_greenfield_still_packs():
    n = _two_routes_with_routers()
    res = solve_allocation(
        n, _hi_qot(),
        [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0}],
        spare_inventory={"A": 2, "Z": 2})
    assert res.status in (SolverStatus.SOLUTION, SolverStatus.PARTIAL)
    assert res.placements
    # empty net -> LPE edges vanish -> new-lightpath-only placements (no grooming)
    assert all(p.reused_lightpaths == () for p in res.placements)
    assert all(p.new_lightpaths for p in res.placements)


def test_scarcity_grooms_onto_survivor():
    n = _model_with_survivor_lightpath()
    res = solve_allocation(
        n, _hi_qot(),
        [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0}],
        spare_inventory={"A": 0, "Z": 0})   # transponders exhausted -> no new LP
    # grooms rather than returning a false no_solution
    assert res.status is not SolverStatus.NO_SOLUTION
    assert res.placements
    p = res.placements[0]
    assert p.reused_lightpaths == ("lp-survivor",)  # rode the survivor
    assert p.new_lightpaths == ()                    # lit nothing new
