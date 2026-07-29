"""solve_allocation: weighted, consuming heuristic packer over the layered engine.

Grooms demands onto surviving lightpaths' residual capacity and lights new
lightpaths (one transponder per new-run endpoint) otherwise. Resource exhaustion
is a typed `partial`/`no_solution`, never an exception. These tests preserve the
legacy intents (greenfield consumes transponders; scarce inventory -> partial; no
inventory -> no_solution; protected consumes more and yields two disjoint legs;
protected insufficient -> unplaced) against the new AllocationResult shape.
"""
from multilayer_optical_mcp.model.assets import ROADM
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, SRLG, TransceiverMode, Router,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.allocation import solve_allocation, solve_allocation_model


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
    for node in ("A", "Z"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS("oms-north", "A", "Z", ("roadm_A", "aN1", "fN", "aN2")))
    n.add_oms(OMS("oms-south", "A", "Z", ("roadm_A", "aS1", "fS", "aS2")))
    # The synth-service consumption path needs a router at each demand endpoint.
    n.add_router(Router(id="r_A", site="A"))
    n.add_router(Router(id="r_Z", site="Z"))
    return n


def _hi_qot() -> FakeQot:
    return FakeQot({("oms-north",): 16.0, ("oms-south",): 16.0})


def test_greenfield_places_and_consumes_transponders():
    # Full-mode (400G) demands fill their lightpath, so demand 2 cannot groom onto
    # demand 1's survivor (residual 0) and must light its own new lightpath —
    # greenfield lights two new lightpaths and consumes the inventory.
    n = _two_routes()
    res = solve_allocation(n, _hi_qot(),
                           [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 400.0},
                            {"id": "d2", "src": "A", "dst": "Z", "demand_gbps": 400.0}],
                           spare_inventory={"A": 2, "Z": 2})
    assert res.status is SolverStatus.SOLUTION
    assert len(res.placements) == 2
    assert all(p.reused_lightpaths == () for p in res.placements)   # nothing to groom onto
    assert all(p.new_lightpaths for p in res.placements)            # each lit a lightpath
    assert all(r.mode_id == "400G" for p in res.placements for r in p.new_lightpaths)


def test_scarce_inventory_yields_partial_no_exception():
    # One transponder per site: the higher-weight demand lights the only lightpath,
    # exhausting inventory; the second (full-mode) demand cannot groom onto a filled
    # survivor and has no transponder -> unplaced, not an exception.
    n = _two_routes()
    res = solve_allocation(
        n, _hi_qot(),
        [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 400.0},
         {"id": "d2", "src": "A", "dst": "Z", "demand_gbps": 400.0}],
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
    assert res.unplaced[0] == ("d1", "insufficient transponders")


def test_protected_consumes_four_transponders_and_two_routes():
    # Exactly 2 per site -> enough for one protected demand (working + protection
    # lightpaths, one transponder per new-run endpoint = 2 per site).
    n = _two_routes()
    res, work = solve_allocation_model(n, _hi_qot(),
                           [{"id": "d1", "src": "A", "dst": "Z",
                             "demand_gbps": 100.0, "protected": True}],
                           spare_inventory={"A": 2, "Z": 2})
    assert res.status is SolverStatus.SOLUTION
    p = res.placements[0]
    assert p.new_lightpaths          # working leg lit a lightpath
    assert p.protection_new          # protection leg lit a lightpath
    work_oms = {o for r in p.new_lightpaths for o in r.oms_sequence}
    prot_oms = {o for r in p.protection_new for o in r.oms_sequence}
    assert work_oms and prot_oms and work_oms.isdisjoint(prot_oms)   # disjoint routes

    # Root-cause pin: protection_path must actually be written on the Service, and
    # its IP links must resolve back to protection_new's OMS set.
    svc = work.get_service("d1")
    assert svc.protection_path, "protection leg was provisioned but never stitched onto the Service"
    prot_path_oms = set()
    for ip_id in svc.protection_path:
        lp_id = work.get_ip_link(ip_id).lightpath_id
        prot_path_oms |= set(work.get_lightpath(lp_id).oms_sequence)
    assert prot_path_oms == prot_oms


def test_protected_insufficient_inventory_unplaced():
    n = _two_routes()
    res = solve_allocation(n, _hi_qot(),
                           [{"id": "d1", "src": "A", "dst": "Z",
                             "demand_gbps": 100.0, "protected": True}],
                           spare_inventory={"A": 1, "Z": 1})  # need 2 each
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.unplaced[0] == ("d1", "insufficient transponders")


def test_solve_allocation_threads_one_grid_through_layered_and_placement(monkeypatch):
    """S7-12 fix: build_layered_graph and place_demands must share one
    SpectrumGrid instance per demand placement."""
    import multilayer_optical_mcp.model.multilayer_graph as _mg

    n = _two_routes()
    seen_grids = []
    real_build_spectrum_state = _mg.build_spectrum_state

    def _spy(model, grid):
        seen_grids.append(grid)
        return real_build_spectrum_state(model, grid)

    monkeypatch.setattr(_mg, "build_spectrum_state", _spy)
    solve_allocation(n, _hi_qot(),
                     [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 400.0}],
                     spare_inventory={"A": 2, "Z": 2})

    assert len(seen_grids) >= 2
    assert all(g is seen_grids[0] for g in seen_grids)


def test_pack_records_unplaced_on_service_id_collision():
    """Regression for the audit's Important finding: _pack must not crash
    when a demand id collides with an existing service id -- it must record
    the demand as unplaced with a clear reason."""
    from multilayer_optical_mcp.model.assets import Service

    n = _two_routes()
    n.add_service(Service(id="d1", src_router="r_A", dst_router="r_Z",
                          demand_gbps=10.0, working_path=()))

    demands = [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0}]
    result, _work = solve_allocation_model(n, _hi_qot(), demands, spare_inventory={"A": 10, "Z": 10})
    assert result.unplaced and result.unplaced[0][0] == "d1"
    assert "exist" in result.unplaced[0][1] or "collide" in result.unplaced[0][1]


def test_pack_leaves_no_phantom_service_for_unroutable_demand():
    """Regression for the audit's Important finding: a brand-new demand id
    (no collision) that fails a downstream check -- here, insufficient
    transponder inventory, discovered only after the route is harvested --
    must not leave a phantom Service(working_path=()) registered on `work`.
    It must show up only in AllocationResult.unplaced."""
    n = _two_routes()
    demands = [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0}]
    result, work = solve_allocation_model(
        n, _hi_qot(), demands, spare_inventory={"A": 0, "Z": 0})

    assert result.status is SolverStatus.NO_SOLUTION
    assert result.placements == ()
    assert result.unplaced == (("d1", "insufficient transponders"),)
    assert "d1" not in {s.id for s in work.list_services()}


def test_pack_leaves_no_phantom_service_for_unreachable_demand():
    """Same regression, via the 'no feasible route' branch: src and dst have
    routers but no OMS path connects them at all."""
    n = NetworkModel(modes=_modes())
    for node in ("A", "Z"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_router(Router(id="r_A", site="A"))
    n.add_router(Router(id="r_Z", site="Z"))
    # No OMS at all between A and Z: any route harvest must come up empty.

    demands = [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0}]
    result, work = solve_allocation_model(
        n, _hi_qot(), demands, spare_inventory={"A": 10, "Z": 10})

    assert result.status is SolverStatus.NO_SOLUTION
    assert result.placements == ()
    assert result.unplaced == (("d1", "no feasible route"),)
    assert "d1" not in {s.id for s in work.list_services()}
