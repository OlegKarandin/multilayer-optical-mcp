"""Step-4 solver tests: routing + disjointness over the optical OMS graph.

Reuses the two-disjoint-route topology shape from test_exposure.py: two
node-disjoint OMS segments A->B (oms-north over fiber-north, oms-south over
fiber-south). Solver outcomes are typed (SolverStatus), never exceptions.
"""
from __future__ import annotations

import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, Router, IPLink, Service,
    SRLG, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.solvers import (
    SolverStatus, RoutingResult, DisjointnessResult,
    compute_paths, check_disjointness, compute_disjoint_paths,
)
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph


def _model_two_paths() -> NetworkModel:
    """Two node-disjoint OMS routes A->B. Mirrors test_exposure.py."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    for amp_id in ("ampA1", "ampA2", "ampB1", "ampB2"):
        n.add_amplifier(Amplifier(id=amp_id, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber-north", a_end="ampA1", z_end="ampA2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fiber-south", a_end="ampB1", z_end="ampB2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_oms(OMS(id="oms-north", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "ampA1", "fiber-north", "ampA2")))
    n.add_oms(OMS(id="oms-south", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "ampB1", "fiber-south", "ampB2")))
    n.add_lightpath(Lightpath(id="lp-north", oms_sequence=("oms-north",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_lightpath(Lightpath(id="lp-south", oms_sequence=("oms-south",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp-north"))
    n.add_ip_link(IPLink(id="ip2", a_router="R1", z_router="R2",
                         lightpath_id="lp-south"))
    n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                          demand_gbps=10.0,
                          working_path=("ip1",), protection_path=("ip2",)))
    return n


# ------------------------------------------- directed OMS (importer both-directions)

def _model_importer_single_span() -> NetworkModel:
    """Importer-built model of ONE bidirectional span 1<->2. The importer emits
    two directed OMS (oms_1_2, oms_2_1) with physically separate fibers/amps."""
    modes = ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ])
    graph = {
        "nodes": [{"id": "1"}, {"id": "2"}],
        "edges": [{"src": "1", "dst": "2", "length_km": 80.0}],
    }
    return model_from_abstract_graph(graph, modes=modes)


def test_compute_paths_directed_returns_only_travel_direction_oms():
    """A->B enumeration must yield the forward OMS only, never the reverse-
    direction OMS of the same span (which belongs to B->A traffic)."""
    n = _model_importer_single_span()
    res = compute_paths(n, "1", "2", k=4)
    assert res.status is SolverStatus.SOLUTION
    found = {p.oms_sequence for p in res.paths}
    assert found == {("oms_1_2",)}, found        # not oms_2_1


def test_compute_disjoint_paths_rejects_two_directions_of_one_span():
    """The two directions of a single span are NOT a valid disjoint pair: they
    share the same physical duct. With only one span there is no disjoint
    pair -> typed NO_SOLUTION (never (oms_1_2,) + (oms_2_1,))."""
    n = _model_importer_single_span()
    res = compute_disjoint_paths(n, "1", "2", basis="physical", level="link",
                                 best_effort=False)
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.path_a is None and res.path_b is None


def _model_parallels_plus_distinct_route(n_parallels: int) -> NetworkModel:
    """`n_parallels` parallel A->B OMS all sharing SRLG 'srlg-trunk', plus a
    topologically distinct A->C->B route that is SRLG-disjoint from them. The
    only disjoint pair is (any trunk parallel) + (A->C->B)."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for node in ("A", "B", "C"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    trunk_fibers = []
    for i in range(n_parallels):
        a1, a2 = f"pa{i}1", f"pa{i}2"
        fib = f"pfib{i}"
        n.add_amplifier(Amplifier(id=a1, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
        n.add_amplifier(Amplifier(id=a2, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
        n.add_fiber(Fiber(id=fib, a_end=a1, z_end=a2, length_km=80.0, type_variety="SSMF"))
        n.add_oms(OMS(id=f"oms-p{i}", src_node_id="A", dst_node_id="B",
                      elements=("roadm_A", a1, fib, a2)))
        trunk_fibers.append(fib)
    n.add_srlg(SRLG(id="srlg-trunk", asset_ids=tuple(trunk_fibers)))
    # distinct route A->C->B (two hops), SRLG-free
    for a in ("acA", "acZ", "cbA", "cbZ"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fib-AC", a_end="acA", z_end="acZ", length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fib-CB", a_end="cbA", z_end="cbZ", length_km=80.0, type_variety="SSMF"))
    n.add_oms(OMS(id="oms-AC", src_node_id="A", dst_node_id="C", elements=("roadm_A", "acA", "fib-AC", "acZ")))
    n.add_oms(OMS(id="oms-CB", src_node_id="C", dst_node_id="B", elements=("roadm_C", "cbA", "fib-CB", "cbZ")))
    return n


def test_compute_disjoint_paths_not_starved_by_parallels():
    """A distinct disjoint route must be found even when >cap parallels on an
    earlier node path would otherwise consume the whole candidate window."""
    n = _model_parallels_plus_distinct_route(n_parallels=33)   # > _DISJOINT_CANDIDATE_CAP
    res = compute_disjoint_paths(n, "A", "B", basis="srlg", level="srlg",
                                 best_effort=False)
    assert res.status is SolverStatus.SOLUTION
    assert res.disjoint is True
    pair = {res.path_a.oms_sequence, res.path_b.oms_sequence}
    assert ("oms-AC", "oms-CB") in pair       # the distinct route entered the scan


# --------------------------------------------------------------- compute_paths

def test_compute_paths_returns_both_routes():
    n = _model_two_paths()
    res = compute_paths(n, "A", "B", k=2)
    assert isinstance(res, RoutingResult)
    assert res.status is SolverStatus.SOLUTION
    found = {p.oms_sequence for p in res.paths}
    assert found == {("oms-north",), ("oms-south",)}


def test_compute_paths_respects_k():
    n = _model_two_paths()
    res = compute_paths(n, "A", "B", k=1)
    assert res.status is SolverStatus.SOLUTION
    assert len(res.paths) == 1


def test_compute_paths_disconnected_dst_is_typed_no_solution():
    """No path must be a typed NO_SOLUTION, never a raised exception."""
    n = _model_two_paths()
    res = compute_paths(n, "A", "C-not-in-graph", k=2)
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.paths == ()


def test_compute_paths_is_deterministic():
    n = _model_two_paths()
    a = compute_paths(n, "A", "B", k=2)
    b = compute_paths(n, "A", "B", k=2)
    assert [p.oms_sequence for p in a.paths] == [p.oms_sequence for p in b.paths]


# ----------------------------------------------------------- check_disjointness

def test_check_disjointness_physical_disjoint():
    n = _model_two_paths()
    res = check_disjointness(n, ("oms-north",), ("oms-south",),
                             basis="physical", level="link")
    assert isinstance(res, DisjointnessResult)
    assert res.disjoint is True
    assert res.shared_assets == ()
    assert res.shared_groups == ()


def test_check_disjointness_same_path_not_disjoint():
    n = _model_two_paths()
    res = check_disjointness(n, ("oms-north",), ("oms-north",),
                             basis="physical", level="link")
    assert res.disjoint is False
    assert "fiber-north" in res.shared_assets


def test_check_disjointness_exhaustive_always_true():
    """Task D: check_disjointness audits two already-given paths -- there is
    no search and no cap to truncate, so `exhaustive` must always default to
    True, unaffected by the new field added for compute_disjoint_paths."""
    n = _model_two_paths()
    res = check_disjointness(n, ("oms-north",), ("oms-south",),
                             basis="physical", level="link")
    assert res.exhaustive is True


def test_check_disjointness_risk_group_is_the_latent_correlation_catch():
    """Scenario 1: physically-disjoint pair, freshly-injected risk group spans
    both, so the *same* pair is no longer disjoint under the risk_group basis."""
    n = _model_two_paths()
    n.define_risk_group(rg_id="rg-storm-cone",
                        asset_ids=("fiber-north", "fiber-south"))
    phys = check_disjointness(n, ("oms-north",), ("oms-south",),
                              basis="physical", level="link")
    assert phys.disjoint is True
    rg = check_disjointness(n, ("oms-north",), ("oms-south",),
                            basis="risk_group", level="risk_group")
    assert rg.disjoint is False
    assert rg.shared_groups == ("rg-storm-cone",)


def test_check_disjointness_union_is_intersection_of_constraints():
    """union basis = disjoint under ALL selected bases. Physically disjoint but
    sharing a risk group -> not union-disjoint."""
    n = _model_two_paths()
    n.define_risk_group(rg_id="rg-storm-cone",
                        asset_ids=("fiber-north", "fiber-south"))
    res = check_disjointness(n, ("oms-north",), ("oms-south",),
                             basis="union", level="link")
    assert res.disjoint is False
    assert res.shared_groups == ("rg-storm-cone",)
    assert res.shared_assets == ()


def test_check_disjointness_srlg_basis():
    n = _model_two_paths()
    n.add_srlg(SRLG(id="srlg-shared-duct",
                    asset_ids=("fiber-north", "fiber-south")))
    res = check_disjointness(n, ("oms-north",), ("oms-south",),
                             basis="srlg", level="srlg")
    assert res.disjoint is False
    assert res.shared_groups == ("srlg-shared-duct",)


def _model_shared_interior_node() -> NetworkModel:
    """A-M1-B (path_a's true route) and A-M2-M1-B (path_b's true route via a
    span-DISTINCT parallel M1->B leg, omsM1B2) -- both genuinely transit node
    M1, but never share a fiber/amp/OMS, so only a correct node-level read
    (not any accidental link-level overlap) can catch the correlation.
    Mirrors test_multilayer_disjoint.py's diamond+hybrid fixture (the already-
    fixed layered-engine sibling of this bug) minus the layered-graph/
    Placement machinery -- check_disjointness only needs raw OMS-sequences."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for node in ("A", "B", "M1", "M2"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    for amp_id in ("aAM1a", "aAM1b", "aM1Ba", "aM1Bb", "aAM2a", "aAM2b",
                   "aM2M1a", "aM2M1b", "aM1Ba2", "aM1Bb2"):
        n.add_amplifier(Amplifier(id=amp_id, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAM1", a_end="aAM1a", z_end="aAM1b",
                      length_km=60.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fM1B", a_end="aM1Ba", z_end="aM1Bb",
                      length_km=60.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fAM2", a_end="aAM2a", z_end="aAM2b",
                      length_km=60.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fM2M1", a_end="aM2M1a", z_end="aM2M1b",
                      length_km=40.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fM1B2", a_end="aM1Ba2", z_end="aM1Bb2",
                      length_km=60.0, type_variety="SSMF"))
    n.add_oms(OMS(id="omsAM1", src_node_id="A", dst_node_id="M1",
                  elements=("roadm_A", "aAM1a", "fAM1", "aAM1b")))
    n.add_oms(OMS(id="omsM1B", src_node_id="M1", dst_node_id="B",
                  elements=("roadm_M1", "aM1Ba", "fM1B", "aM1Bb")))
    n.add_oms(OMS(id="omsAM2", src_node_id="A", dst_node_id="M2",
                  elements=("roadm_A", "aAM2a", "fAM2", "aAM2b")))
    n.add_oms(OMS(id="omsM2M1", src_node_id="M2", dst_node_id="M1",
                  elements=("roadm_M2", "aM2M1a", "fM2M1", "aM2M1b")))
    n.add_oms(OMS(id="omsM1B2", src_node_id="M1", dst_node_id="B",
                  elements=("roadm_M1", "aM1Ba2", "fM1B2", "aM1Bb2")))
    return n


def test_check_disjointness_endpoints_kwarg_fixes_shared_interior_node():
    """Regression for the flat-engine sibling of the ALREADY-FIXED layered-
    engine bug (multilayer_disjoint.placement_footprint_keys/disjoint_pairs'
    `endpoints` kwarg, see test_multilayer_disjoint.py's
    test_hybrid_placement_endpoint_exclusion_needs_explicit_endpoints and
    test_route_service_and_check_disjointness_agree_on_hybrid_placements).

    path_a's true physical route is A->M1->B, but is handed to
    check_disjointness with its legs concatenated in "M1->B leg first" order
    -- exactly how a caller assembling a reused leg + a new-run leg in
    storage (not travel) order would build it. Without explicit endpoints,
    check_disjointness's positional inference reads path_a's own mandated
    endpoints off oms_sequence[0].src/oms_sequence[-1].dst = {omsM1B.src=M1,
    omsAM1.dst=M1} = {M1} -- wrongly excluding the one node the two paths
    actually share, and wrongly keeping A/B (not this ordering's own
    endpoints) in its key set instead. That flips the verdict to a falsely
    certified disjoint=True at level='node'."""
    n = _model_shared_interior_node()
    path_a = ("omsM1B", "omsAM1")                    # true route A->M1->B, wrong order
    path_b = ("omsAM2", "omsM2M1", "omsM1B2")         # true route A->M2->M1->B, correct order

    broken = check_disjointness(n, path_a, path_b, basis="physical", level="node")
    # Characterizes the CURRENT no-endpoints-supplied behavior (positional inference,
    # still wrong for out-of-order paths). This assertion documents a known limitation,
    # not a contract -- if a future change narrows or removes the no-endpoints blind
    # spot, update/remove this assertion rather than treating its failure as a regression.
    assert broken.disjoint is True, broken.shared_assets   # the bug: falsely disjoint

    fixed = check_disjointness(n, path_a, path_b, basis="physical", level="node",
                               endpoints_a=("A", "B"), endpoints_b=("A", "B"))
    assert fixed.disjoint is False
    assert "M1" in fixed.shared_assets            # node-level: shared interior node
    assert "roadm_M1" in fixed.shared_assets       # phys floor: shared terminal ROADM


def test_compute_paths_severed_by_avoid_is_typed_no_solution():
    """Regression for the audit's Critical NetworkXNoPath-escapes finding: a
    legitimate avoid constraint that severs the ONLY route between src/dst
    must return a typed NO_SOLUTION, not raise networkx.NetworkXNoPath."""
    n = _model_importer_single_span()   # A single OMS oms_1_2 between 1 and 2
    res = compute_paths(n, "1", "2", k=3,
                        constraints={"avoid": {"assets": ["oms_1_2"]}})
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.paths == ()


def test_compute_paths_excludes_oms_crossing_failed_asset():
    """Regression for the audit's failed-assets-never-consulted finding: a
    fiber marked failed via inject_failure must be excluded from the search
    graph, not merely surfaced later as a -inf QoT sentinel. Pre-fix, k=2 on
    this topology returns BOTH oms-north and oms-south (see
    test_compute_paths_returns_both_routes) -- the counter-example this test
    guards against is oms-north (which crosses the now-failed fiber-north)
    reappearing in the result."""
    from multilayer_optical_mcp.model.whatif import inject_failure
    n = _model_two_paths()
    inject_failure(n, ("fiber-north",))
    res = compute_paths(n, "A", "B", k=2)
    assert res.status is SolverStatus.SOLUTION
    found = {p.oms_sequence for p in res.paths}
    assert found == {("oms-south",)}, found   # oms-north must NOT reappear


def test_compute_paths_all_routes_failed_is_typed_no_solution():
    """Both routes failed -> typed NO_SOLUTION, never an exception and never a
    solution that crosses a failed fiber."""
    from multilayer_optical_mcp.model.whatif import inject_failure
    n = _model_two_paths()
    inject_failure(n, ("fiber-north", "fiber-south"))
    res = compute_paths(n, "A", "B", k=2)
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.paths == ()


def test_compute_disjoint_paths_excludes_pair_crossing_failed_asset():
    """Regression for the same finding at compute_disjoint_paths: with only
    two node-disjoint routes and one failed, no disjoint PAIR can be formed
    (only one usable route survives) -- must be typed NO_SOLUTION, never a
    'solution' pairing a survivor with the failed route. Pre-fix, this
    topology has exactly the physically-disjoint pair (oms-north, oms-south)
    (see test_compute_disjoint_paths_physical_finds_pair); the counter-example
    guarded against is that pair (or any pair containing oms-north)
    reappearing after fiber-north is failed."""
    from multilayer_optical_mcp.model.whatif import inject_failure
    n = _model_two_paths()
    inject_failure(n, ("fiber-north",))
    res = compute_disjoint_paths(n, "A", "B", basis="physical", level="link",
                                 best_effort=False)
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.path_a is None and res.path_b is None

    # best_effort=True must not manufacture a pair out of thin air either --
    # there is only one surviving candidate route, so no pair exists at all.
    res_be = compute_disjoint_paths(n, "A", "B", basis="physical", level="link",
                                    best_effort=True)
    assert res_be.status is SolverStatus.NO_SOLUTION


def test_compute_disjoint_paths_finds_survivor_pair_when_third_route_available():
    """With a THIRD, topologically distinct route available, failing one of
    two SRLG-sharing trunk parallels must not just collapse to NO_SOLUTION --
    the solver must route around the failure and still find a genuine
    disjoint pair among the survivors (the remaining trunk parallel + the
    distinct A->C->B route), and that pair must never include the failed
    oms-p0/pfib0."""
    from multilayer_optical_mcp.model.whatif import inject_failure
    n = _model_parallels_plus_distinct_route(n_parallels=2)   # oms-p0, oms-p1 (trunk) + oms-AC/oms-CB
    inject_failure(n, ("pfib0",))   # fail one of the two trunk parallels' fiber
    res = compute_disjoint_paths(n, "A", "B", basis="srlg", level="srlg",
                                 best_effort=False)
    assert res.status is SolverStatus.SOLUTION
    assert res.disjoint is True
    pair = {res.path_a.oms_sequence, res.path_b.oms_sequence}
    assert pair == {("oms-p1",), ("oms-AC", "oms-CB")}
    assert "oms-p0" not in {oms for path in pair for oms in path}


def test_solve_rsa_places_routable_demand_despite_unroutable_sibling():
    """The same bug at the allocation layer: a batch with one routable demand
    and one demand severed by avoid must place the routable one, recording
    the other as unplaced -- not raise and lose the whole batch."""
    from multilayer_optical_mcp.model.allocation import solve_rsa
    n = _model_two_paths()   # A<->B via oms-north AND oms-south (two routes)

    def fake_qot(*, oms_sequence, direction, mode_id, loading):
        from multilayer_optical_mcp.model.qot import QoTState
        return QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=8.0)

    demands = [
        {"id": "d-ok", "src": "A", "dst": "B"},
        {"id": "d-severed", "src": "A", "dst": "B",
         "constraints": {"avoid": {"assets": ["oms-north", "oms-south"]}}},
    ]
    res = solve_rsa(n, fake_qot, demands)
    assert res.status is SolverStatus.PARTIAL
    placed_ids = {p.demand_id for p in res.placements}
    unplaced_ids = {did for did, _ in res.unplaced}
    assert placed_ids == {"d-ok"}
    assert unplaced_ids == {"d-severed"}


def test_solve_rsa_protected_demand_honors_avoid_constraint():
    """Regression: `_place_protected` must thread a demand's own `constraints`
    into `compute_disjoint_paths`, exactly as `_place_unprotected` already
    threads them into `compute_paths`.

    `_model_two_paths()` has exactly two A->B routes (oms-north, oms-south),
    so the ONLY possible disjoint working+protection pair is
    (oms-north, oms-south). Pre-fix, `_place_protected` ignored `constraints`
    and would still find and USE that pair even though the demand asked to
    avoid oms-north -- a real wrong-answer counter-example, not just an
    exception check. Post-fix, oms-north is pruned before disjoint-pair
    search, leaving only one surviving route -- no pair of two can be formed,
    so the demand must come back typed `unplaced` (no exception, no pair that
    silently crosses the avoided asset)."""
    from multilayer_optical_mcp.model.allocation import solve_rsa
    n = _model_two_paths()   # A<->B via oms-north AND oms-south (two routes)

    def fake_qot(*, oms_sequence, direction, mode_id, loading):
        from multilayer_optical_mcp.model.qot import QoTState
        return QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=8.0)

    demands = [
        {"id": "d-prot", "src": "A", "dst": "B", "protected": True,
         "constraints": {"avoid": {"assets": ["oms-north"]}}},
    ]
    res = solve_rsa(n, fake_qot, demands)
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.placements == ()
    unplaced_ids = {did for did, _ in res.unplaced}
    assert unplaced_ids == {"d-prot"}

    # Sanity check on the premise: without the avoid constraint, the SAME
    # topology/demand-shape DOES place the protected pair using oms-north --
    # confirms the NO_SOLUTION above is caused by honoring `avoid`, not by
    # some unrelated infeasibility.
    demands_unconstrained = [
        {"id": "d-prot", "src": "A", "dst": "B", "protected": True},
    ]
    res_unconstrained = solve_rsa(n, fake_qot, demands_unconstrained)
    assert res_unconstrained.status is SolverStatus.SOLUTION
    placement = res_unconstrained.placements[0]
    used_oms = set(placement.working.oms_path.oms_sequence) | \
        set(placement.protection.oms_path.oms_sequence)
    assert used_oms == {"oms-north", "oms-south"}


# ------------------------------------------------------- compute_disjoint_paths

def test_compute_disjoint_paths_physical_finds_pair():
    n = _model_two_paths()
    res = compute_disjoint_paths(n, "A", "B", basis="physical", level="link",
                                 best_effort=False)
    assert res.status is SolverStatus.SOLUTION
    assert res.disjoint is True
    pair = {res.path_a.oms_sequence, res.path_b.oms_sequence}
    assert pair == {("oms-north",), ("oms-south",)}


def test_compute_disjoint_paths_no_solution_when_all_share_and_not_best_effort():
    n = _model_two_paths()
    n.define_risk_group(rg_id="rg-storm-cone",
                        asset_ids=("fiber-north", "fiber-south"))
    res = compute_disjoint_paths(n, "A", "B", basis="risk_group",
                                 level="risk_group", best_effort=False)
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.path_a is None and res.path_b is None


def test_compute_disjoint_paths_best_effort_returns_partial_min_overlap():
    n = _model_two_paths()
    n.define_risk_group(rg_id="rg-storm-cone",
                        asset_ids=("fiber-north", "fiber-south"))
    res = compute_disjoint_paths(n, "A", "B", basis="risk_group",
                                 level="risk_group", best_effort=True)
    assert res.status is SolverStatus.PARTIAL
    assert res.disjoint is False
    pair = {res.path_a.oms_sequence, res.path_b.oms_sequence}
    assert pair == {("oms-north",), ("oms-south",)}
    assert res.shared_groups == ("rg-storm-cone",)


def _model_exponential_parallels_plus_bypass(n_hops: int, parallels_per_hop: int) -> NetworkModel:
    """A node-chain A->n1->...->n(k-1)->B with `parallels_per_hop` parallel OMS
    on EVERY hop (one node path alone emits parallels_per_hop**n_hops combos),
    plus a fully node-disjoint, one-hop-longer bypass route sharing no
    intermediate node with the chain. With parallels_per_hop=2, n_hops=10:
    the chain alone emits 2**10=1024 == _DISJOINT_EMISSION_CAP combos, so a
    GLOBAL emission counter never reaches the bypass node path at all."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))

    chain_nodes = ["A"] + [f"n{i}" for i in range(1, n_hops)] + ["B"]
    bypass_nodes = ["A"] + [f"b{i}" for i in range(1, n_hops + 1)] + ["B"]
    for node in set(chain_nodes) | set(bypass_nodes):
        n.add_roadm(ROADM(id=f"roadm_{node}"))

    for h in range(n_hops):
        u, v = chain_nodes[h], chain_nodes[h + 1]
        for p in range(parallels_per_hop):
            a1, a2 = f"ch{h}_{p}_1", f"ch{h}_{p}_2"
            fib = f"chfib{h}_{p}"
            n.add_amplifier(Amplifier(id=a1, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
            n.add_amplifier(Amplifier(id=a2, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
            n.add_fiber(Fiber(id=fib, a_end=a1, z_end=a2, length_km=80.0, type_variety="SSMF"))
            n.add_oms(OMS(id=f"oms-ch{h}-{p}", src_node_id=u, dst_node_id=v,
                          elements=(f"roadm_{u}", a1, fib, a2)))

    for h in range(len(bypass_nodes) - 1):
        u, v = bypass_nodes[h], bypass_nodes[h + 1]
        a1, a2 = f"by{h}_1", f"by{h}_2"
        fib = f"byfib{h}"
        n.add_amplifier(Amplifier(id=a1, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
        n.add_amplifier(Amplifier(id=a2, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
        n.add_fiber(Fiber(id=fib, a_end=a1, z_end=a2, length_km=80.0, type_variety="SSMF"))
        n.add_oms(OMS(id=f"oms-by{h}", src_node_id=u, dst_node_id=v,
                      elements=(f"roadm_{u}", a1, fib, a2)))
    return n


def test_compute_disjoint_paths_finds_bypass_despite_exponential_parallels():
    """Regression for the audit's Critical emission-cap-starvation finding."""
    from multilayer_optical_mcp.model.solvers import _DISJOINT_EMISSION_CAP
    n_hops, parallels_per_hop = 10, 2
    # Task-8 fix reviewer's Minor finding: this test's premise depends on
    # 2**n_hops == _DISJOINT_EMISSION_CAP (the chain alone must exactly fill
    # the global budget) -- assert the relationship explicitly so the test
    # doesn't silently stop exercising the starvation path if the constant
    # changes.
    assert parallels_per_hop ** n_hops == _DISJOINT_EMISSION_CAP
    n = _model_exponential_parallels_plus_bypass(n_hops=n_hops, parallels_per_hop=parallels_per_hop)
    res = compute_disjoint_paths(n, "A", "B", basis="physical", level="link",
                                 best_effort=False)
    assert res.status is SolverStatus.SOLUTION
    assert res.disjoint is True
    # Minor finding: also assert the bypass route is actually the one found,
    # not merely that *some* disjoint pair exists.
    expected_bypass = tuple(f"oms-by{h}" for h in range(n_hops + 1))
    pair = {res.path_a.oms_sequence, res.path_b.oms_sequence}
    assert expected_bypass in pair


def test_compute_disjoint_paths_exhaustive_false_under_genuine_emission_cap_truncation():
    """Task D: `exhaustive` must honestly report False when the search was
    genuinely truncated. Reuses the exponential-parallels-plus-bypass fixture:
    the chain node path alone emits exactly 2**10=1024==_DISJOINT_EMISSION_CAP
    combos, and the bypass node path emits one more on top -- a true candidate
    space of 1025, one more than the cap can hold. `_enumerate_oms_paths` is
    hard-capped at `_DISJOINT_EMISSION_CAP` emissions, so `cands` in
    compute_disjoint_paths necessarily comes back at exactly 1024 items
    (never 1025): the search was really cut short, not merely brushing the
    cap by coincidence, and `exhaustive` must reflect that honestly."""
    from multilayer_optical_mcp.model.solvers import _DISJOINT_EMISSION_CAP
    n_hops, parallels_per_hop = 10, 2
    assert parallels_per_hop ** n_hops == _DISJOINT_EMISSION_CAP
    n = _model_exponential_parallels_plus_bypass(n_hops=n_hops, parallels_per_hop=parallels_per_hop)
    res = compute_disjoint_paths(n, "A", "B", basis="physical", level="link",
                                 best_effort=False)
    # Sanity: the true candidate space (1024 chain combos + 1 bypass combo)
    # exceeds the cap, so this is a genuine truncation, not a coincidental
    # exact fill -- the SOLUTION status alone doesn't prove that.
    assert res.status is SolverStatus.SOLUTION
    assert res.exhaustive is False


def test_compute_disjoint_paths_exhaustive_true_within_cap():
    """Regression guard: a small topology whose candidate space is nowhere
    near either cap must report exhaustive=True (the default, un-truncated
    case)."""
    n = _model_two_paths()
    res = compute_disjoint_paths(n, "A", "B", basis="physical", level="link",
                                 best_effort=False)
    assert res.status is SolverStatus.SOLUTION
    assert res.exhaustive is True


def _model_single_route_alternating_srlg_planes(n_hops: int, parallels_per_hop: int = 2) -> NetworkModel:
    """A SINGLE node-chain A->n1->...->n(k-1)->B (no alternate node path) with
    `parallels_per_hop` parallel OMS on EVERY hop, where parallel index 0 at
    every hop belongs to SRLG 'srlg-plane0' and parallel index 1 belongs to
    SRLG 'srlg-plane1' -- mirroring the classic working/protection-over-
    diverse-conduits setup (e.g. two physically diverse fiber plants running
    alongside the same node route). The ONLY SRLG-disjoint pair is the two
    "pure" full-route combinations (all-plane0 vs all-plane1); every other
    combination touches both SRLGs and so shares a group with everything.

    In `itertools.product`'s odometer order (last hop varies fastest), the
    all-plane0 combo is index 0 (first emitted) but the all-plane1 combo is
    the LAST of parallels_per_hop**n_hops combos -- so a per-node-path
    emission cap smaller than that count truncates it away even though this
    is the ONLY node path and the 1024-emission global budget sits almost
    entirely unused."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))

    chain_nodes = ["A"] + [f"n{i}" for i in range(1, n_hops)] + ["B"]
    for node in chain_nodes:
        n.add_roadm(ROADM(id=f"roadm_{node}"))

    plane0_fibers: List[str] = []
    plane1_fibers: List[str] = []
    for h in range(n_hops):
        u, v = chain_nodes[h], chain_nodes[h + 1]
        for p in range(parallels_per_hop):
            a1, a2 = f"ch{h}_{p}_1", f"ch{h}_{p}_2"
            fib = f"chfib{h}_{p}"
            n.add_amplifier(Amplifier(id=a1, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
            n.add_amplifier(Amplifier(id=a2, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
            n.add_fiber(Fiber(id=fib, a_end=a1, z_end=a2, length_km=80.0, type_variety="SSMF"))
            n.add_oms(OMS(id=f"oms-ch{h}-{p}", src_node_id=u, dst_node_id=v,
                          elements=(f"roadm_{u}", a1, fib, a2)))
            if p == 0:
                plane0_fibers.append(fib)
            elif p == 1:
                plane1_fibers.append(fib)
    n.add_srlg(SRLG(id="srlg-plane0", asset_ids=tuple(plane0_fibers)))
    n.add_srlg(SRLG(id="srlg-plane1", asset_ids=tuple(plane1_fibers)))
    return n


def test_compute_disjoint_paths_finds_srlg_disjoint_extremes_in_single_node_path():
    """Regression for the Task-8-fix reviewer's finding: a per-node-path
    emission cap that truncates ODOMETER-order emission (rather than the
    node-path count) can throw away a single node path's own most-diverse
    combinations when they sit near the far end of itertools.product's
    enumeration, even though this is the ONLY node path and the global
    emission budget is nowhere near exhausted."""
    n_hops = 6
    n = _model_single_route_alternating_srlg_planes(n_hops=n_hops, parallels_per_hop=2)
    res = compute_disjoint_paths(n, "A", "B", basis="srlg", level="link",
                                 best_effort=False)
    assert res.status is SolverStatus.SOLUTION
    assert res.disjoint is True
    expected_plane0 = tuple(f"oms-ch{h}-0" for h in range(n_hops))
    expected_plane1 = tuple(f"oms-ch{h}-1" for h in range(n_hops))
    pair = {res.path_a.oms_sequence, res.path_b.oms_sequence}
    assert pair == {expected_plane0, expected_plane1}


def _model_single_route_length_misaligned_srlg_planes(n_hops: int, parallels_per_hop: int = 2) -> NetworkModel:
    """Same single-node-path/two-SRLG-plane shape as
    _model_single_route_alternating_srlg_planes, but the per-hop physical
    fiber LENGTH is deliberately alternated so plane0's OMS sorts to index 0
    on even hops and index 1 on odd hops (and plane1 the mirror image) under
    `_oms_between`'s (length, id) sort with weight="length" -- mirroring the
    second-round reviewer's "conduit A shorter on even hops, longer on odd
    hops" repro. Because the parallel-index assignment is NOT index-aligned
    across hops, neither `_hop_combos`' "all index 0" nor "all index 1"
    diagonal combo is the genuinely SRLG-disjoint pure-plane0/pure-plane1
    pair -- both diagonals alternate between plane0 and plane1 hop-to-hop and
    so touch both SRLGs. Only draining a node path's combo iterator past its
    first per_path_cap share (budget-draining resumption) reaches the true
    pair."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))

    chain_nodes = ["A"] + [f"n{i}" for i in range(1, n_hops)] + ["B"]
    for node in chain_nodes:
        n.add_roadm(ROADM(id=f"roadm_{node}"))

    plane0_fibers: List[str] = []
    plane1_fibers: List[str] = []
    for h in range(n_hops):
        u, v = chain_nodes[h], chain_nodes[h + 1]
        # Alternate which plane is physically shorter on this hop, so the
        # (length, id) sort in _oms_between puts a DIFFERENT plane at index 0
        # depending on hop parity -- purely a length artifact, nothing to do
        # with SRLG/plane membership.
        len0, len1 = (80.0, 90.0) if h % 2 == 0 else (90.0, 80.0)
        for p, length_km in ((0, len0), (1, len1)):
            a1, a2 = f"ch{h}_{p}_1", f"ch{h}_{p}_2"
            fib = f"chfib{h}_{p}"
            n.add_amplifier(Amplifier(id=a1, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
            n.add_amplifier(Amplifier(id=a2, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
            n.add_fiber(Fiber(id=fib, a_end=a1, z_end=a2, length_km=length_km, type_variety="SSMF"))
            n.add_oms(OMS(id=f"oms-ch{h}-{p}", src_node_id=u, dst_node_id=v,
                          elements=(f"roadm_{u}", a1, fib, a2)))
            if p == 0:
                plane0_fibers.append(fib)
            else:
                plane1_fibers.append(fib)
    n.add_srlg(SRLG(id="srlg-plane0", asset_ids=tuple(plane0_fibers)))
    n.add_srlg(SRLG(id="srlg-plane1", asset_ids=tuple(plane1_fibers)))
    return n


def test_compute_disjoint_paths_finds_srlg_disjoint_extremes_despite_length_misaligned_parallel_order():
    """Regression for the SECOND round of Task-8-fix review: diagonal-first
    ordering only closes the INDEX-ALIGNED subset of the truncation bug. When
    parallel-option ordering is NOT index-aligned across hops -- here,
    weight="length" sorting by physical fiber length puts a different plane
    at index 0 on even vs. odd hops, with nothing to do with SRLG membership
    -- the diagonal ("all index 0"/"all index 1") combos are NOT the
    genuinely disjoint pair, and without budget-draining resumption the true
    pure-plane0/pure-plane1 pair falls outside the per-node-path cap even
    though the global emission budget (1024) is nowhere near exhausted at
    5-7 hops (32/64/128 combos)."""
    for n_hops in (5, 6, 7):
        n = _model_single_route_length_misaligned_srlg_planes(n_hops=n_hops, parallels_per_hop=2)
        res = compute_disjoint_paths(n, "A", "B", basis="srlg", level="link",
                                     best_effort=False, weight="length")
        assert res.status is SolverStatus.SOLUTION, f"n_hops={n_hops}"
        assert res.disjoint is True, f"n_hops={n_hops}"
        expected_plane0 = tuple(f"oms-ch{h}-0" for h in range(n_hops))
        expected_plane1 = tuple(f"oms-ch{h}-1" for h in range(n_hops))
        pair = {res.path_a.oms_sequence, res.path_b.oms_sequence}
        assert pair == {expected_plane0, expected_plane1}, f"n_hops={n_hops}"


# ------------------------------------------ round-robin resumption (round 3)

def _model_trunk_and_planes_shared_stem(
    trunk_hops: int, planes_hops: int, parallels_per_hop: int = 2,
) -> NetworkModel:
    """Reviewer's exact THIRD-round repro: a shared stem A->S, then from S two
    parallel S->B routes:

    - "trunk": `trunk_hops` hops x `parallels_per_hop` parallels, physically
      SHORT per hop (2km) so it sorts FIRST under weight="length" node-path
      ordering. With trunk_hops=10, parallels=2 this is 1024 combos -- the
      entire global emission budget (_DISJOINT_EMISSION_CAP).
    - "planes": `planes_hops` hops, length-misaligned parallel SRLG planes
      (mirrors `_model_single_route_length_misaligned_srlg_planes`) so its
      only genuinely SRLG-disjoint pair (pure-plane0 vs pure-plane1) is NOT
      the diagonal-first extremes and needs the whole remaining per-node-path
      budget drained to reach -- but it is physically SHORT (mostly 80-90km
      per hop) relative to nothing else, so it sorts SECOND, well after trunk.

    Every trunk fiber (both parallel indices, every hop) is a member of BOTH
    srlg-plane0 AND srlg-plane1 (in addition to planes' own fibers each being
    a member of exactly one) -- so ANY trunk combo always carries both plane
    labels and can never be SRLG-disjoint from anything (another trunk combo,
    or any planes combo, pure or mixed). The ONLY possible SRLG-disjoint pair
    in the whole candidate set is planes' own pure-plane0 vs pure-plane1
    extremes -- reachable only if planes' iterator is drained past its own
    first-pass share, which is exactly what resumption must deliver fairly."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_roadm(ROADM(id="roadm_A"))
    n.add_roadm(ROADM(id="roadm_S"))
    n.add_roadm(ROADM(id="roadm_B"))

    # Shared stem A->S: single OMS, SRLG-free (physical sharing at this hop
    # must not itself register as an SRLG correlation for this test's basis).
    n.add_amplifier(Amplifier(id="stemA1", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_amplifier(Amplifier(id="stemA2", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fib-stem", a_end="stemA1", z_end="stemA2", length_km=1.0, type_variety="SSMF"))
    n.add_oms(OMS(id="oms-stem", src_node_id="A", dst_node_id="S",
                  elements=("roadm_A", "stemA1", "fib-stem", "stemA2")))

    trunk_fibers: List[str] = []
    trunk_chain = ["S"] + [f"tk{i}" for i in range(1, trunk_hops)] + ["B"]
    for node in trunk_chain[1:-1]:
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    for h in range(trunk_hops):
        u, v = trunk_chain[h], trunk_chain[h + 1]
        for p in range(parallels_per_hop):
            a1, a2 = f"tk{h}_{p}_1", f"tk{h}_{p}_2"
            fib = f"tkfib{h}_{p}"
            n.add_amplifier(Amplifier(id=a1, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
            n.add_amplifier(Amplifier(id=a2, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
            n.add_fiber(Fiber(id=fib, a_end=a1, z_end=a2, length_km=2.0, type_variety="SSMF"))
            n.add_oms(OMS(id=f"oms-tk{h}-{p}", src_node_id=u, dst_node_id=v,
                          elements=(f"roadm_{u}", a1, fib, a2)))
            trunk_fibers.append(fib)

    plane0_fibers: List[str] = []
    plane1_fibers: List[str] = []
    planes_chain = ["S"] + [f"pl{i}" for i in range(1, planes_hops)] + ["B"]
    for node in planes_chain[1:-1]:
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    for h in range(planes_hops):
        u, v = planes_chain[h], planes_chain[h + 1]
        len0, len1 = (80.0, 90.0) if h % 2 == 0 else (90.0, 80.0)
        for p, length_km in ((0, len0), (1, len1)):
            a1, a2 = f"pl{h}_{p}_1", f"pl{h}_{p}_2"
            fib = f"plfib{h}_{p}"
            n.add_amplifier(Amplifier(id=a1, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
            n.add_amplifier(Amplifier(id=a2, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
            n.add_fiber(Fiber(id=fib, a_end=a1, z_end=a2, length_km=length_km, type_variety="SSMF"))
            n.add_oms(OMS(id=f"oms-pl{h}-{p}", src_node_id=u, dst_node_id=v,
                          elements=(f"roadm_{u}", a1, fib, a2)))
            if p == 0:
                plane0_fibers.append(fib)
            else:
                plane1_fibers.append(fib)

    n.add_srlg(SRLG(id="srlg-plane0", asset_ids=tuple(plane0_fibers) + tuple(trunk_fibers)))
    n.add_srlg(SRLG(id="srlg-plane1", asset_ids=tuple(plane1_fibers) + tuple(trunk_fibers)))
    return n


def test_compute_disjoint_paths_round_robin_resumption_closes_greedy_drain_starvation():
    """Regression for the THIRD round of Task-8-fix review: the round-2 fix's
    "budget-draining resumption" reintroduced the SAME wrong-answer bug class
    a third time by draining parked node paths SEQUENTIALLY (one fully to
    exhaustion/budget-out, then the next) with no fairness discipline.

    Reviewer's exact repro: shared stem A->S, then two parallel S->B routes --
    a combo-rich "trunk" (10 hops x 2 parallels = 1024 combos, sorts first
    under weight="length") and a shorter, length-misaligned "planes" route (6
    hops x 2 parallels = 64 combos) whose only SRLG-disjoint pair (pure-plane0
    vs pure-plane1) sits past its own first-pass share and needs resumption to
    reach (mirrors the round-2 fixture/test above). Under the OLD sequential
    resumption, trunk is parked first and its 992 remaining combos alone
    consume the ENTIRE leftover global budget (32+960=992 additional
    emissions) before planes -- parked second -- is ever resumed at all, so
    the genuinely disjoint pair is never in the candidate set: a false
    NO_SOLUTION even though only 96 of the 1024-emission budget was actually
    needed. Round-robin resumption (one next() per still-live parked iterator
    per sweep) gives planes a fair, interleaved share of the resumption
    budget, fully draining its 32 remaining combos within the first 32 sweeps
    -- long before trunk or the global budget is exhausted."""
    trunk_hops, planes_hops = 10, 6
    n = _model_trunk_and_planes_shared_stem(trunk_hops=trunk_hops, planes_hops=planes_hops,
                                            parallels_per_hop=2)
    res = compute_disjoint_paths(n, "A", "B", basis="srlg", level="link",
                                 best_effort=False, weight="length")
    assert res.status is SolverStatus.SOLUTION
    assert res.disjoint is True
    expected_plane0 = ("oms-stem",) + tuple(f"oms-pl{h}-0" for h in range(planes_hops))
    expected_plane1 = ("oms-stem",) + tuple(f"oms-pl{h}-1" for h in range(planes_hops))
    pair = {res.path_a.oms_sequence, res.path_b.oms_sequence}
    assert pair == {expected_plane0, expected_plane1}


# ---------------------------- saturated-budget diagonal-first (Important #2)

def _model_saturated_budget_diagonal_extremes(
    n_hops: int = 6, parallels_per_hop: int = 2, n_filler_routes: int = 31,
) -> NetworkModel:
    """32 fully node-disjoint parallel A->B routes: 1 "target" route plus
    `n_filler_routes` fillers, each with `parallels_per_hop` parallel OMS on
    every hop (n_hops=6, parallels=2 -> 64 combos per route -- more than
    per_path_cap=32, so every route's first pass is genuinely truncated).
    With exactly 32 routes and per_path_cap=32
    (== _DISJOINT_EMISSION_CAP // _DISJOINT_CANDIDATE_CAP), the first pass
    alone emits exactly 32*32=1024==_DISJOINT_EMISSION_CAP -- the global
    budget is entirely exhausted WITHIN the first pass, so the second-pass
    resumption (round-robin or the old sequential drain, doesn't matter which)
    never runs at all. This isolates diagonal-first ordering as the ONLY
    mechanism that can still deliver a genuinely disjoint pair.

    The target route's fibers carry parallel-index-PURE SRLG membership
    (index 0 -> srlg-0 only, index 1 -> srlg-1 only, mirroring
    `_model_single_route_alternating_srlg_planes`), while EVERY filler
    route's fibers (both parallel indices, all hops) belong to BOTH srlg-0
    AND srlg-1 -- so a filler combo always carries {srlg-0, srlg-1} and can
    never be disjoint from anything (itself, another filler, or either of the
    target's extremes), and the target's own "mixed" combos (touching both
    indices across hops) also always carry both labels. The ONLY possible
    disjoint pair in the whole candidate set is the target route's own two
    pure extremes (all-index-0 vs all-index-1) -- reachable ONLY because
    diagonal_first puts both within the first TWO emissions of the target's
    own per-path budget, regardless of where in the 32-route processing
    order the target sits."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_roadm(ROADM(id="roadm_A"))
    n.add_roadm(ROADM(id="roadm_B"))

    target_fibers0: List[str] = []
    target_fibers1: List[str] = []
    filler_fibers: List[str] = []

    def _build_route(route_key: str, both_labels: bool) -> None:
        chain = ["A"] + [f"{route_key}_{i}" for i in range(1, n_hops)] + ["B"]
        for node in chain[1:-1]:
            n.add_roadm(ROADM(id=f"roadm_{node}"))
        for h in range(n_hops):
            u, v = chain[h], chain[h + 1]
            for p in range(parallels_per_hop):
                a1, a2 = f"{route_key}_ch{h}_{p}_1", f"{route_key}_ch{h}_{p}_2"
                fib = f"{route_key}_chfib{h}_{p}"
                n.add_amplifier(Amplifier(id=a1, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
                n.add_amplifier(Amplifier(id=a2, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
                n.add_fiber(Fiber(id=fib, a_end=a1, z_end=a2, length_km=80.0, type_variety="SSMF"))
                n.add_oms(OMS(id=f"oms-{route_key}-{h}-{p}", src_node_id=u, dst_node_id=v,
                              elements=(f"roadm_{u}", a1, fib, a2)))
                if both_labels:
                    filler_fibers.append(fib)
                elif p == 0:
                    target_fibers0.append(fib)
                else:
                    target_fibers1.append(fib)

    _build_route("tg", both_labels=False)
    for i in range(n_filler_routes):
        _build_route(f"fl{i}", both_labels=True)

    n.add_srlg(SRLG(id="srlg-0", asset_ids=tuple(target_fibers0) + tuple(filler_fibers)))
    n.add_srlg(SRLG(id="srlg-1", asset_ids=tuple(target_fibers1) + tuple(filler_fibers)))
    return n


def test_compute_disjoint_paths_finds_srlg_disjoint_extremes_under_saturated_budget():
    """Important #2 (third-round review): the round-1 index-aligned test
    (`test_compute_disjoint_paths_finds_srlg_disjoint_extremes_in_single_node_path`
    above) no longer isolates diagonal-first ordering as its guard, because
    with only ONE node path ever parked, budget-draining resumption --
    sequential OR round-robin -- drains the ENTIRE remaining product
    regardless of internal ordering, so the "all-index-1" extreme would be
    found via resumption alone even if diagonal_first did nothing at all.
    Diagonal-first is load-bearing ONLY when the global emission budget is
    exhausted entirely within the first pass (>=32 node paths each with >=32
    combos, so 32*32==1024==k leaves nothing for resumption to run at all).
    This test builds exactly that regime and confirms the target route's own
    SRLG-pure index-aligned extremes are still found."""
    from multilayer_optical_mcp.model.solvers import _DISJOINT_EMISSION_CAP, _DISJOINT_CANDIDATE_CAP
    n_hops, parallels_per_hop, n_filler_routes = 6, 2, 31
    assert n_filler_routes + 1 == _DISJOINT_CANDIDATE_CAP
    per_path_cap = _DISJOINT_EMISSION_CAP // _DISJOINT_CANDIDATE_CAP
    assert _DISJOINT_CANDIDATE_CAP * per_path_cap == _DISJOINT_EMISSION_CAP
    assert parallels_per_hop ** n_hops > per_path_cap   # every route's first pass is truncated

    n = _model_saturated_budget_diagonal_extremes(n_hops=n_hops, parallels_per_hop=parallels_per_hop,
                                                   n_filler_routes=n_filler_routes)
    res = compute_disjoint_paths(n, "A", "B", basis="srlg", level="link", best_effort=False)
    assert res.status is SolverStatus.SOLUTION
    assert res.disjoint is True
    expected_pure0 = tuple(f"oms-tg-{h}-0" for h in range(n_hops))
    expected_pure1 = tuple(f"oms-tg-{h}-1" for h in range(n_hops))
    pair = {res.path_a.oms_sequence, res.path_b.oms_sequence}
    assert pair == {expected_pure0, expected_pure1}
