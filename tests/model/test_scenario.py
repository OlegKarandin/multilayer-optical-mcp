"""Operating-network builder: solve_allocation_model wrapper (Component C) and
build_operating_network convergence driver (Component B).

All GNPy-free: a FakeQot supplies GSNR by route and the driver's QoT-settle seam
is stubbed, so these exercise the packer/materialization/convergence logic without
the real adapter.
"""
import json
import os
from pathlib import Path

import pytest

from multilayer_optical_mcp.model.assets import (
    ROADM, FiberType, Fiber, Amplifier, OMS, TransceiverMode, Router, IPLink,
    Lightpath,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.spectrum import SpectrumGrid
from multilayer_optical_mcp.model.allocation import (
    solve_allocation, solve_allocation_model,
)
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.ip_routing import simulate_ip_routing
from multilayer_optical_mcp.model import scenario
from multilayer_optical_mcp.model.scenario import build_operating_network


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
    n.add_router(Router(id="r_A", site="A"))
    n.add_router(Router(id="r_Z", site="Z"))
    return n


def _hi_qot() -> FakeQot:
    return FakeQot({("oms-north",): 16.0, ("oms-south",): 16.0})


# ------------------------------------------------------------ Component C parity

def test_solve_allocation_model_matches_solve_allocation_result():
    """The wrapper returns an AllocationResult byte-equal to solve_allocation's,
    plus a loaded `work` model carrying one lightpath per placement's new run."""
    n = _two_routes_with_routers()
    demands = [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0}]
    inv = {"A": 2, "Z": 2}

    bare = solve_allocation(n, _hi_qot(), demands, spare_inventory=dict(inv))
    result, work = solve_allocation_model(n, _hi_qot(), demands, spare_inventory=dict(inv))

    assert result == bare                          # identical typed result
    assert result.status in (SolverStatus.SOLUTION, SolverStatus.PARTIAL)
    # greenfield: one new lightpath was lit and materialized on the returned model
    n_new = sum(len(p.new_lightpaths) for p in result.placements)
    assert len(work.list_lightpaths()) == n_new
    assert n.list_lightpaths() == ()               # ground truth untouched


# ---------------------------------------------------- Component B convergence driver

class ConstQot:
    """Route-agnostic high GSNR: any path clears the 400G threshold."""
    def __init__(self, gsnr=16.0):
        self.g = gsnr

    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        return QoTState(gsnr_db=self.g, osnr_db=30.0, margin_db=0.0)


def _triangle() -> NetworkModel:
    """3-node ring built via the real importer → bidirectional OMS, so demands
    route in both directions and there is path diversity for grooming."""
    graph = {
        "nodes": [{"id": i} for i in range(3)],
        "edges": [
            {"src": 0, "dst": 1, "length_km": 80.0},
            {"src": 1, "dst": 2, "length_km": 80.0},
            {"src": 0, "dst": 2, "length_km": 80.0},
        ],
    }
    return model_from_abstract_graph(graph, modes=_modes())


@pytest.fixture(scope="module")
def built():
    """One convergent build (target mean util 0.5), shared across the checks below —
    the search runs solve_allocation many times, so build once and assert many."""
    m = _triangle()
    res = build_operating_network(
        m, seed=0, qot=ConstQot(), target_mean_util=0.5, max_util_cap=0.95,
        settle=lambda w: None)
    return m, res


def test_converges_within_caps(built):
    ground_truth, res = built
    assert res.report.achieved_mean_util <= 0.5 + 0.05    # never overshoots target
    assert res.report.achieved_max_util <= 0.95           # cap respected
    assert res.model.list_lightpaths()                    # a loaded operating network
    assert ground_truth.list_lightpaths() == ()           # ground truth untouched


def test_low_cap_forces_cap_limited_partial():
    m = _triangle()
    res = build_operating_network(
        m, seed=0, qot=ConstQot(), target_mean_util=0.9, max_util_cap=0.3,
        settle=lambda w: None)
    assert res.report.status is SolverStatus.PARTIAL
    assert res.report.limit == "max_util_cap"
    assert res.report.achieved_max_util <= 0.3


def test_materialized_baseline_has_no_drops(built):
    _ground_truth, res = built
    assert res.report.unplaced_count == 0
    ipr = simulate_ip_routing(res.model)
    assert ipr.dropped_services == ()
    for u in ipr.utilizations:                            # every link capacity known
        assert u.capacity_gbps is not None and u.capacity_gbps > 0.0


# ---------------------------------------------------- pair_density forwarding

def _spy_generate_demands(monkeypatch, recorder):
    """Replace scenario.generate_demands with a spy that records the
    `pair_density` it was called with and returns one trivial demand, so the
    convergence driver stays cheap (a real gravity build is ~minutes — see
    test_converges_within_caps). Proves forwarding without exercising the packer
    at scale."""
    def spy(model, *, seed, scale, pair_density=None, **kw):
        recorder.append(pair_density)
        return [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0}]
    monkeypatch.setattr(scenario, "generate_demands", spy)


def test_pair_density_forwarded_to_generate_demands(monkeypatch):
    """The explicit `pair_density` value reaches `generate_demands` on every
    convergence sample — it is not silently dropped by the builder."""
    seen: list = []
    _spy_generate_demands(monkeypatch, seen)
    build_operating_network(
        _two_routes_with_routers(), seed=0, qot=_hi_qot(),
        target_mean_util=0.5, max_util_cap=0.95, pair_density=0.2,
        settle=lambda w: None)
    assert seen                              # the driver sampled at least once
    assert all(pd == 0.2 for pd in seen)     # and always with the forwarded value


def test_pair_density_defaults_to_none(monkeypatch):
    """Omitting the kwarg forwards `None` — today's full-matrix behavior is
    preserved for existing callers."""
    seen: list = []
    _spy_generate_demands(monkeypatch, seen)
    build_operating_network(
        _two_routes_with_routers(), seed=0, qot=_hi_qot(),
        target_mean_util=0.5, max_util_cap=0.95, settle=lambda w: None)
    assert seen
    assert all(pd is None for pd in seen)


# ------------------------------------------------ real-adapter end-to-end (opt-in)

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    not os.environ.get("MOMCP_RUN_GNPY_E2E"),
    reason="slow real-GNPy build; set MOMCP_RUN_GNPY_E2E=1 to run")
def test_german_17_end_to_end_real_adapter():
    """Full build against the real GNPy adapter: gravity demands → packer →
    materialized clone → QoT settle. Opt-in (slow)."""
    from multilayer_optical_mcp.model.modes import load_modulation_formats
    from multilayer_optical_mcp.model.qot_results import QoTResultStore
    from multilayer_optical_mcp.model.allocation import make_adapter_evaluator

    graph = json.loads((_REPO / "topologies/german_17.json").read_text(encoding="utf-8"))
    modes = load_modulation_formats(_REPO / "modulation_formats.yaml")
    model = model_from_abstract_graph(graph, modes=modes)
    store = QoTResultStore()
    qot = make_adapter_evaluator(model, store)

    res = build_operating_network(
        model, seed=0, qot=qot, target_mean_util=0.4, max_util_cap=0.95,
        max_iters=10, store=store)

    assert res.model.list_lightpaths()                    # a loaded operating network
    assert res.report.status in (SolverStatus.SOLUTION, SolverStatus.PARTIAL)
    assert simulate_ip_routing(res.model).dropped_services == ()
