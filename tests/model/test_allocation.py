"""solve_allocation: greenfield heuristic placement under scarce transponders.

Lights new lightpaths from a per-site transponder count; mode falls out of SNR.
Grooming onto existing lightpaths is Step 5. Resource exhaustion is a typed
`partial`/`no_solution`, never an exception.
"""
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, SRLG, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.solvers import SolverStatus
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


def _two_routes() -> NetworkModel:
    n = NetworkModel(modes=_modes())
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("aN1", "aN2", "aS1", "aS2"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber("fN", "aN1", "aN2", 80.0, "SSMF"))
    n.add_fiber(Fiber("fS", "aS1", "aS2", 120.0, "SSMF"))
    n.add_oms(OMS("oms-north", "A", "Z", ("aN1", "fN", "aN2")))
    n.add_oms(OMS("oms-south", "A", "Z", ("aS1", "fS", "aS2")))
    return n


def _hi_qot() -> FakeQot:
    return FakeQot({("oms-north",): 16.0, ("oms-south",): 16.0})


def test_greenfield_places_and_consumes_transponders():
    n = _two_routes()
    res = solve_allocation(n, _hi_qot(),
                           [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0},
                            {"id": "d2", "src": "A", "dst": "Z", "demand_gbps": 100.0}],
                           spare_inventory={"A": 2, "Z": 2})
    assert res.status is SolverStatus.SOLUTION
    assert len(res.placements) == 2
    assert all(p.working.mode_id == "400G" for p in res.placements)


def test_scarce_inventory_yields_partial_no_exception():
    n = _two_routes()
    res = solve_allocation(
        n, _hi_qot(),
        [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0},
         {"id": "d2", "src": "A", "dst": "Z", "demand_gbps": 100.0}],
        spare_inventory={"A": 1, "Z": 1},
        weights={"d1": 10.0, "d2": 1.0},
    )
    assert res.status is SolverStatus.PARTIAL
    assert [p.demand_id for p in res.placements] == ["d1"]   # higher weight placed
    assert res.unplaced and res.unplaced[0][0] == "d2"


def test_no_inventory_is_no_solution():
    n = _two_routes()
    res = solve_allocation(n, _hi_qot(),
                           [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0}],
                           spare_inventory={"A": 0, "Z": 0})
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.placements == ()


def test_protected_consumes_four_transponders_and_two_routes():
    n = _two_routes()
    # Exactly 2 per site -> enough for one protected demand (2 lightpaths).
    res = solve_allocation(n, _hi_qot(),
                           [{"id": "d1", "src": "A", "dst": "Z",
                             "demand_gbps": 100.0, "protected": True}],
                           spare_inventory={"A": 2, "Z": 2})
    assert res.status is SolverStatus.SOLUTION
    p = res.placements[0]
    assert p.protection is not None
    assert p.working.oms_path.oms_sequence != p.protection.oms_path.oms_sequence


def test_protected_insufficient_inventory_unplaced():
    n = _two_routes()
    res = solve_allocation(n, _hi_qot(),
                           [{"id": "d1", "src": "A", "dst": "Z",
                             "demand_gbps": 100.0, "protected": True}],
                           spare_inventory={"A": 1, "Z": 1})  # need 2 each
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.unplaced[0][0] == "d1"
