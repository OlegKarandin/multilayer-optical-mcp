"""solve_allocation: weighted, consuming heuristic packer over the layered engine.

Grooms demands onto surviving lightpaths' residual capacity and lights new
lightpaths (one transponder per new-run endpoint) otherwise. Resource exhaustion
is a typed `partial`/`no_solution`, never an exception. These tests preserve the
legacy intents (greenfield consumes transponders; scarce inventory -> partial; no
inventory -> no_solution; protected consumes more and yields two disjoint legs;
protected insufficient -> unplaced) against the new AllocationResult shape.
"""
import pytest

from multilayer_optical_mcp.model.assets import ROADM
from multilayer_optical_mcp.model.assets import FiberType, Fiber, Amplifier, OMS, TransceiverMode
from multilayer_optical_mcp.model.ip_assets import Router
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
    from multilayer_optical_mcp.model.ip_assets import Service

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


# --------------------------------------------------------------------------
# Final-review Finding 1: Task 4's cross-lightpath QoT invalidation (any
# lightpath sharing an OMS with a newly-provisioned one has its QoT wiped)
# fires when a LATER provisioning call in the same _pack run invalidates an
# EARLIER call's just-seeded QoT, leaving that earlier lightpath with NO QoT
# state at all. Real solver, real GNPy adapter throughout -- no mocks.


def test_apply_candidate_self_corrects_seed_wiped_by_sibling_run():
    """Mechanism-level reproduction of manifestation (a): TWO new-lightpath
    runs sharing one physical OMS WITHIN A SINGLE apply_candidate call
    (Task 6's confirmed-real co-located-siblings scenario, reusing Task 6's
    own mesh fixture from test_fill_policy.py).

    Final-review Fix 1: apply_candidate now re-applies every seed IT
    collected in one corrective pass before returning, so calling it ALONE
    (no caller-side re-seed needed) already leaves both co-located
    lightpaths with correct QoT state. This closes manifestation (a) in the
    general case (not just via _pack's external accumulation) and fixes
    score_candidate/score_pair's scoring under-count, since those scorers
    call apply_candidate/provision_new_runs directly and never saw _pack's
    post-loop accumulation.

    Formerly (pre-fix) this test pinned the RAW bug: apply_candidate alone
    left exactly one of the two co-located lightpaths with no QoT state,
    fixed only by the caller re-applying the returned `seeded` pairs. That
    is no longer the contract -- apply_candidate's own return value is
    still provided (and still needed by _pack for the CROSS-DEMAND case,
    see below), but the immediate post-call state is now already correct."""
    from multilayer_optical_mcp.model.ip_assets import Service
    from multilayer_optical_mcp.model.multilayer_graph import build_layered_graph
    from multilayer_optical_mcp.model.allocation import make_adapter_evaluator
    from multilayer_optical_mcp.model.qot_results import QoTResultStore
    from multilayer_optical_mcp.model.spectrum import FillPolicy
    from multilayer_optical_mcp.model import objective as objective_mod
    from tests.model.test_fill_policy import (
        _shared_oms_mesh_model, _runs_using, place_demands,
    )

    n, grid = _shared_oms_mesh_model()
    qot = make_adapter_evaluator(n, QoTResultStore())
    g = build_layered_graph(n, grid=grid)
    res = place_demands(n, g, qot, src="C", dst="Z", demand_gbps=100.0,
                        policy="new_only", k=20, grid=grid,
                        fill_policy=FillPolicy.ACTUAL)
    target = next((p for p in res if len(_runs_using(p, "oms_C_M")) == 2), None)
    assert target is not None, "expected a placement with two new runs sharing oms_C_M"

    work = n.clone()
    svc = Service(id="svc-a", src_router="router_C", dst_router="router_Z",
                 demand_gbps=100.0, working_path=())
    work.add_service(svc)

    seeded = objective_mod.apply_candidate(work, target, svc)

    # apply_candidate provisioned 2 new lightpaths sharing oms_C_M; the
    # second one's provisioning invalidates the first's just-seeded QoT
    # (Task 4's cross-lightpath invalidation) DURING the call, but Fix 1's
    # corrective pass at the end of apply_candidate's own body re-applies
    # both seeds before returning -- so NEITHER is left missing.
    assert len(seeded) == 2, "expected exactly the 2 new co-located runs to be seeded"
    missing = [lp_id for lp_id, _st in seeded
              if _no_qot_state(work, lp_id)]
    assert missing == [], (
        f"apply_candidate alone should leave zero co-located lightpaths "
        f"without QoT state post-fix; got missing={missing}")

    # The returned `seeded` tuple is still correct/complete -- allocation.py's
    # _pack still needs it for the CROSS-DEMAND case (a later demand's
    # provisioning wiping an earlier demand's already-correct lightpath,
    # which no single apply_candidate call can see or fix on its own).
    for lp_id, state in seeded:
        assert not _no_qot_state(work, lp_id)


def _no_qot_state(model, lp_id) -> bool:
    try:
        model.get_qot_state(lp_id)
    except LookupError:
        return True
    return False


def _line_three_node_model():
    """C-M-Z line topology (real GNPy adapter, two 80 km OMS both
    directions). d1 (C->Z) expresses straight through M as ONE lightpath
    spanning (oms_C_M, oms_M_Z); d2 (C->M) lights its own lightpath on
    oms_C_M alone. Different IP endpoints (Z vs M) mean d2 can never groom
    onto d1's lightpath, so both ALWAYS light fresh lightpaths that share
    physical OMS oms_C_M -- the cross-demand manifestation (c): d2's later
    provisioning (Task 4's cross-lightpath invalidation) wipes d1's
    just-seeded QoT within one `_pack` run, deterministically, independent of
    any grooming/residual-capacity edge case."""
    from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph

    graph = {
        "nodes": [{"id": "C"}, {"id": "M"}, {"id": "Z"}],
        "edges": [
            {"src": "C", "dst": "M", "length_km": 80.0},
            {"src": "M", "dst": "Z", "length_km": 80.0},
        ],
    }
    return model_from_abstract_graph(graph, modes=_modes())


def _place_cross_demand_scenario():
    """Runs the real solver on `_line_three_node_model` and returns
    `(result, work, lp_d1, lp_d2)`. Shared by the two tests below."""
    from multilayer_optical_mcp.model.allocation import make_adapter_evaluator
    from multilayer_optical_mcp.model.qot_results import QoTResultStore

    n = _line_three_node_model()
    qot = make_adapter_evaluator(n, QoTResultStore())
    demands = [
        {"id": "d1", "src": "C", "dst": "Z", "demand_gbps": 50.0},
        {"id": "d2", "src": "C", "dst": "M", "demand_gbps": 50.0},
    ]
    result, work = solve_allocation_model(
        n, qot, demands, spare_inventory={"C": 10, "M": 10, "Z": 10})

    assert result.status is SolverStatus.SOLUTION
    assert result.unplaced == ()
    by_demand = {p.demand_id: p for p in result.placements}
    assert set(by_demand) == {"d1", "d2"}
    # Confirms the scenario actually landed on the shared-OMS manifestation
    # this test exists to exercise, not some other placement shape: d1 is a
    # single run spanning both OMS hops, d2 a single run on oms_C_M alone,
    # and they share oms_C_M.
    d1_runs = by_demand["d1"].new_lightpaths
    d2_runs = by_demand["d2"].new_lightpaths
    assert len(d1_runs) == 1 and d1_runs[0].oms_sequence == ("oms_C_M", "oms_M_Z")
    assert len(d2_runs) == 1 and d2_runs[0].oms_sequence == ("oms_C_M",)

    lp_d1 = next(lp.id for lp in work.list_lightpaths()
                if lp.oms_sequence == ("oms_C_M", "oms_M_Z"))
    lp_d2 = next(lp.id for lp in work.list_lightpaths()
                if lp.oms_sequence == ("oms_C_M",))
    return result, work, lp_d1, lp_d2


def test_pack_reseeds_qot_for_colocated_new_lightpaths_across_demands():
    """End-to-end through the real solve_allocation_model/_pack. BEFORE
    Finding 1's fix (verified by temporarily disabling _pack's corrective
    re-seed pass and re-running this exact scenario): d2's provisioning
    shares oms_C_M with d1's already-provisioned, already-seeded lightpath,
    so Task 4's cross-lightpath invalidation wipes d1's QoT and nothing ever
    re-seeds it -- work.get_qot_state(lp_d1) raised LookupError. AFTER the
    fix, both new lightpaths carry valid QoT state in the returned `work`,
    matching solve_allocation_model's own 'byte-for-byte the state a real
    commit would produce' contract."""
    result, work, lp_d1, lp_d2 = _place_cross_demand_scenario()

    for lp_id in (lp_d1, lp_d2):
        state = work.get_qot_state(lp_id)   # must not raise LookupError
        assert state.margin_db == state.margin_db  # finite, sanity (NaN != NaN)


def test_pack_reseed_keeps_total_margin_from_skipping_colocated_lightpath():
    """evaluate_objective's total_margin loop silently `continue`s past any
    lightpath with no recorded QoT state. Confirms that, post-fix, NEITHER
    of the two co-located lightpaths from the cross-demand scenario above is
    silently skipped: total_margin must equal the direct sum of both
    lightpaths' own margin_db (there are no other lightpaths in this model)."""
    from multilayer_optical_mcp.model.objective import evaluate_objective

    result, work, lp_d1, lp_d2 = _place_cross_demand_scenario()

    assert {lp.id for lp in work.list_lightpaths()} == {lp_d1, lp_d2}
    direct_sum = (work.get_qot_state(lp_d1).margin_db
                 + work.get_qot_state(lp_d2).margin_db)

    obj = evaluate_objective(work)
    # Every lightpath in the model has a QoT state post-fix, so total_margin
    # is exactly their sum -- no silent skip.
    assert obj.total_margin == pytest.approx(direct_sum)


def test_pack_per_iteration_reseed_lets_later_demand_groom_onto_earlier_lightpath():
    """Final-review Fix 2: manifestations (b)/(c). Before this fix, _pack's
    corrective re-seed ran ONCE after the whole demand loop -- so a lightpath
    wiped mid-loop by a co-located sibling read as cap=0 to _residual_gbps
    (multilayer_graph) for every demand routed in between, and a later
    demand that could have groomed onto it lit a redundant new lightpath
    instead (a real, wrong placement decision, not just a stale reading).

    Three demands over `_line_three_node_model` (C-M-Z, both 80 km OMS):
    d1 (C->Z, 50 Gbps) lights a lightpath spanning both OMS hops; d2
    (C->M, 50 Gbps) lights its own lightpath on oms_C_M alone, sharing
    physical OMS with d1's and wiping d1's just-seeded QoT (Task 4's
    cross-lightpath invalidation -- same mechanism as the cross-demand test
    above). d3 (C->Z, 10 Gbps) has the SAME endpoints as d1 and easily fits
    in d1's residual capacity (100G-or-better mode minus 50 Gbps already
    carried) -- but only if d1's QoT reads correctly when d3 routes, which
    now happens because the per-iteration re-seed (not just the final,
    post-loop one) fixes d1 immediately after d2's iteration completes."""
    from multilayer_optical_mcp.model.solvers import SolverStatus

    n = _line_three_node_model()
    demands = [
        {"id": "d1", "src": "C", "dst": "Z", "demand_gbps": 50.0},
        {"id": "d2", "src": "C", "dst": "M", "demand_gbps": 50.0},
        {"id": "d3", "src": "C", "dst": "Z", "demand_gbps": 10.0},
    ]
    from multilayer_optical_mcp.model.allocation import make_adapter_evaluator
    from multilayer_optical_mcp.model.qot_results import QoTResultStore

    qot = make_adapter_evaluator(n, QoTResultStore())
    result, work = solve_allocation_model(
        n, qot, demands, spare_inventory={"C": 10, "M": 10, "Z": 10})

    assert result.status is SolverStatus.SOLUTION
    assert result.unplaced == ()
    by_demand = {p.demand_id: p for p in result.placements}

    # d3 must groom onto d1's already-lit C->Z lightpath -- no new lightpath,
    # no new transponders -- which is only possible if d1's QoT was already
    # corrected (non-zero residual) by the time d3's routing ran.
    d3 = by_demand["d3"]
    assert d3.new_lightpaths == (), (
        "d3 should groom onto d1's survivor lightpath, not light a new one "
        f"(new_lightpaths={d3.new_lightpaths!r}) -- indicates d1's QoT was "
        "still wiped (cap=0) when d3 routed")
    assert d3.reused_lightpaths != ()

    # Exactly 2 lightpaths (d1's, d2's) exist in the final model -- d3 added
    # none. Confirms the "fewer new lightpaths / transponders" claim directly,
    # not just via the reused_lightpaths/new_lightpaths field on d3 alone.
    assert len(work.list_lightpaths()) == 2
