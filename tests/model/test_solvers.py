"""Step-4 solver tests: routing + disjointness over the optical OMS graph.

Reuses the two-disjoint-route topology shape from test_exposure.py: two
node-disjoint OMS segments A->B (oms-north over fiber-north, oms-south over
fiber-south). Solver outcomes are typed (SolverStatus), never exceptions.
"""
from __future__ import annotations

import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, Router, IPLink, Service,
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
    for amp_id in ("ampA1", "ampA2", "ampB1", "ampB2"):
        n.add_amplifier(Amplifier(id=amp_id, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber-north", a_end="ampA1", z_end="ampA2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fiber-south", a_end="ampB1", z_end="ampB2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_oms(OMS(id="oms-north", src_node_id="A", dst_node_id="B",
                  elements=("ampA1", "fiber-north", "ampA2")))
    n.add_oms(OMS(id="oms-south", src_node_id="A", dst_node_id="B",
                  elements=("ampB1", "fiber-south", "ampB2")))
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
