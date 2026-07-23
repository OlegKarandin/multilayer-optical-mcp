"""placement_common: the three helpers previously duplicated (or, for
_forbidden_assets/_lever, defined-but-only-imported-elsewhere) across
allocation.py, route_service.py, and restoration.py."""
import pytest

from multilayer_optical_mcp.model.assets import SRLG
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.multilayer_graph import Placement, NewLightpathRun
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.placement_common import (
    _forbidden_assets, _lever, _status,
)


def _model() -> NetworkModel:
    return NetworkModel(modes=ModeRegistry([]))


def test_forbidden_assets_empty_avoid_is_empty():
    assert _forbidden_assets(_model(), None) == frozenset()
    assert _forbidden_assets(_model(), {}) == frozenset()


def test_forbidden_assets_explicit_assets_pass_through():
    assert _forbidden_assets(_model(), {"assets": ["fAB", "fBC"]}) == frozenset(
        {"fAB", "fBC"})


def test_forbidden_assets_expands_named_srlg_and_risk_group():
    n = _model()
    n.add_srlg(SRLG(id="srlg1", asset_ids=("fAB", "fBC")))
    n.define_risk_group("rg1", ("fXY",))
    bad = _forbidden_assets(n, {"assets": ["fZZ"], "risk_groups": ["srlg1", "rg1"]})
    assert bad == frozenset({"fZZ", "fAB", "fBC", "fXY"})


def test_lever_hybrid_optical_reroute_ip_reroute():
    new_run = NewLightpathRun(("oms1",), 0, "100G", 15.0, 100.0)
    hybrid = Placement(reused_lightpaths=("lp1",), new_lightpaths=(new_run,),
                       restored_gbps=100.0, shortfall_gbps=0.0)
    optical = Placement(reused_lightpaths=(), new_lightpaths=(new_run,),
                        restored_gbps=100.0, shortfall_gbps=0.0)
    ip = Placement(reused_lightpaths=("lp1",), new_lightpaths=(),
                   restored_gbps=100.0, shortfall_gbps=0.0)
    assert _lever(hybrid) == "hybrid"
    assert _lever(optical) == "optical_reroute"
    assert _lever(ip) == "ip_reroute"


def test_status_solution_partial_no_solution():
    assert _status(has_any=True, fully_satisfied=True) == SolverStatus.SOLUTION
    assert _status(has_any=True, fully_satisfied=False) == SolverStatus.PARTIAL
    assert _status(has_any=False, fully_satisfied=False) == SolverStatus.NO_SOLUTION


def test_status_empty_input_ordering_matches_each_original_caller():
    # allocation._status(n_placed=0, n_unplaced=0) used to return SOLUTION
    # (0 unplaced out of 0 demands is vacuously "fully satisfied") -- the
    # shared helper must reproduce that when fully_satisfied=True even though
    # has_any=False.
    assert _status(has_any=False, fully_satisfied=True) == SolverStatus.SOLUTION
    # route_service._status([]) / restoration's inline check used to return
    # NO_SOLUTION for zero candidates -- any() over an empty list is False,
    # so fully_satisfied is False too, and this must stay NO_SOLUTION.
    assert _status(has_any=False, fully_satisfied=False) == SolverStatus.NO_SOLUTION
