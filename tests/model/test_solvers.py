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


def test_compute_paths_severed_by_avoid_is_typed_no_solution():
    """Regression for the audit's Critical NetworkXNoPath-escapes finding: a
    legitimate avoid constraint that severs the ONLY route between src/dst
    must return a typed NO_SOLUTION, not raise networkx.NetworkXNoPath."""
    n = _model_importer_single_span()   # A single OMS oms_1_2 between 1 and 2
    res = compute_paths(n, "1", "2", k=3,
                        constraints={"avoid": {"assets": ["oms_1_2"]}})
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.paths == ()


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
