"""solve_rsa: route-first (by length), spectrum first-fit, mode-from-SNR.

Placement combinatorics use a deterministic fake QotEvaluator so the routing /
spectrum / mode logic is exercised without paying GNPy per case. One real-GNPy
integration case pins the seam to the adapter on toy_2route.json.
"""
from pathlib import Path

import pytest

from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, SRLG, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.allocation import solve_rsa

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------- fakes / fixtures

class FakeQot:
    """Returns a fixed GSNR per OMS-route (mode-independent, as in the single-
    transponder-type model). Ignores loading/direction beyond route identity."""
    def __init__(self, gsnr_by_route):
        self._g = {tuple(k): v for k, v in gsnr_by_route.items()}

    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        gsnr = self._g[tuple(oms_sequence)]
        return QoTState(gsnr_db=gsnr, osnr_db=gsnr + 5.0, margin_db=0.0)


def _modes() -> ModeRegistry:
    return ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=5.0,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
        TransceiverMode(id="200G", bitrate_gbps=200.0, required_gsnr_db=10.0,
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


def _single_route() -> NetworkModel:
    n = NetworkModel(modes=_modes())
    n.register_fiber_type(FiberType("SSMF", 0.2))
    n.add_amplifier(Amplifier(id="a1", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber("f1", "a1", "a1b", 80.0, "SSMF"))
    n.add_oms(OMS("oms-AZ", "A", "Z", ("a1", "f1")))
    return n


# ------------------------------------------------------------------- mode-from-SNR

def test_picks_highest_bitrate_feasible_mode():
    n = _single_route()
    qot = FakeQot({("oms-AZ",): 12.0})  # clears 100G(5) & 200G(10), not 400G(15)
    res = solve_rsa(n, qot, [{"id": "d1", "src": "A", "dst": "Z"}])
    assert res.status is SolverStatus.SOLUTION
    assert res.placements[0].working.mode_id == "200G"


def test_no_feasible_mode_is_unplaced_not_raised():
    n = _single_route()
    qot = FakeQot({("oms-AZ",): 3.0})  # below every threshold
    res = solve_rsa(n, qot, [{"id": "d1", "src": "A", "dst": "Z"}])
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.unplaced and res.unplaced[0][0] == "d1"


def test_required_gbps_filters_modes():
    n = _single_route()
    qot = FakeQot({("oms-AZ",): 12.0})  # best feasible is 200G
    res = solve_rsa(n, qot, [{"id": "d1", "src": "A", "dst": "Z", "required_gbps": 400.0}])
    assert res.status is SolverStatus.NO_SOLUTION  # 200G < 400G demand


# ------------------------------------------------------------------- spectrum

def test_disjoint_routes_can_share_a_slot():
    n = _two_routes()
    qot = FakeQot({("oms-north",): 16.0, ("oms-south",): 16.0})
    res = solve_rsa(n, qot, [
        {"id": "d1", "src": "A", "dst": "Z"},
        {"id": "d2", "src": "A", "dst": "Z"},
    ])
    assert res.status is SolverStatus.SOLUTION
    slots = {p.demand_id: p.working.slot_index for p in res.placements}
    routes = {p.demand_id: p.working.oms_path.oms_sequence for p in res.placements}
    assert routes["d1"] != routes["d2"]      # took different routes
    assert slots["d1"] == slots["d2"] == 0   # ... so both fit slot 0


def test_shared_route_forces_different_slots():
    n = _single_route()
    qot = FakeQot({("oms-AZ",): 16.0})
    res = solve_rsa(n, qot, [
        {"id": "d1", "src": "A", "dst": "Z"},
        {"id": "d2", "src": "A", "dst": "Z"},
    ])
    assert res.status is SolverStatus.SOLUTION
    slots = sorted(p.working.slot_index for p in res.placements)
    assert slots == [0, 1]


# ------------------------------------------------------------------- protection (both bases)

def test_protected_demand_physical_disjoint_pair():
    n = _two_routes()
    qot = FakeQot({("oms-north",): 16.0, ("oms-south",): 16.0})
    res = solve_rsa(n, qot, [{"id": "d1", "src": "A", "dst": "Z", "protected": True}])
    assert res.status is SolverStatus.SOLUTION
    p = res.placements[0]
    assert p.protection is not None
    assert p.working.oms_path.oms_sequence != p.protection.oms_path.oms_sequence


def test_protected_demand_srlg_basis_blocks_when_srlg_spans_both():
    n = _two_routes()
    n.add_srlg(SRLG(id="srlg-duct", asset_ids=("fN", "fS")))
    qot = FakeQot({("oms-north",): 16.0, ("oms-south",): 16.0})
    res = solve_rsa(n, qot, [{
        "id": "d1", "src": "A", "dst": "Z", "protected": True,
        "constraints": {"basis": "srlg", "level": "srlg"},
    }])
    assert res.status is SolverStatus.NO_SOLUTION  # both routes share the SRLG


# ------------------------------------------------------------------- real GNPy

def test_real_gnpy_integration_places_with_adapter_mode():
    """One real-GNPy case on a synthesized bidirectional diamond: the length
    objective picks the shorter of two node-disjoint routes, and the delivered
    mode falls out of the adapter's real (forward+backward) GSNR, not a pre-pick.

    Routes go through distinct intermediate nodes (A-M-Z / A-N-Z) so every OMS
    has a unique (src,dst) and its paired reverse OMS resolves unambiguously."""
    import math
    from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
    from multilayer_optical_mcp.model.allocation import make_adapter_evaluator
    from multilayer_optical_mcp.model.qot_results import QoTResultStore

    graph = {
        "nodes": [{"id": "A"}, {"id": "M"}, {"id": "N"}, {"id": "Z"}],
        "edges": [
            {"src": "A", "dst": "M", "length_km": 80.0},
            {"src": "M", "dst": "Z", "length_km": 80.0},
            {"src": "A", "dst": "N", "length_km": 120.0},
            {"src": "N", "dst": "Z", "length_km": 120.0},
        ],
    }
    n = model_from_abstract_graph(graph, modes=_modes())
    # No topo_path: the adapter synthesizes the bidirectional GNPy network.
    qot = make_adapter_evaluator(n, QoTResultStore())
    res = solve_rsa(n, qot, [{"id": "d1", "src": "A", "dst": "Z"}], objective="shortest")
    assert res.status is SolverStatus.SOLUTION
    p = res.placements[0]
    # North (A-M-Z, 160 km) is shorter than south (A-N-Z, 240 km) -> chosen.
    assert p.working.oms_path.oms_sequence == ("oms_A_M", "oms_M_Z")
    # Mode is whatever the real GSNR supports (>= the lowest mode's requirement).
    assert math.isfinite(p.working.gsnr_db) and p.working.gsnr_db >= 5.0
    assert p.working.mode_id in {"100G", "200G", "400G"}
