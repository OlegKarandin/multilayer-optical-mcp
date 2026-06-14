# tests/model/test_avoidance.py
"""Avoidance: route over survivors by pruning forbidden OMS before enumeration.
Reuses the two-parallel-route shape (oms-north / oms-south, A->B)."""
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, SRLG, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.solvers import SolverStatus, compute_paths


def _two_parallel() -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9)]))
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("aN1", "aN2", "aS1", "aS2"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber("fiber-north", "aN1", "aN2", 80.0, "SSMF"))
    n.add_fiber(Fiber("fiber-south", "aS1", "aS2", 80.0, "SSMF"))
    n.add_oms(OMS("oms-north", "A", "B", ("aN1", "fiber-north", "aN2")))
    n.add_oms(OMS("oms-south", "A", "B", ("aS1", "fiber-south", "aS2")))
    return n


def test_avoid_prunes_forbidden_oms_keeps_survivor():
    n = _two_parallel()
    res = compute_paths(n, "A", "B", k=4,
                        constraints={"avoid": {"assets": ["fiber-north"]}})
    assert res.status is SolverStatus.SOLUTION
    found = {p.oms_sequence for p in res.paths}
    assert found == {("oms-south",)}            # north pruned, south survives


def test_avoid_parallel_in_different_srlg_keeps_the_other():
    """Pruning is per-OMS-edge: avoiding an SRLG that contains only the north
    fiber must not remove the south parallel."""
    n = _two_parallel()
    n.add_srlg(SRLG(id="srlg-north", asset_ids=("fiber-north",)))
    res = compute_paths(n, "A", "B", k=4,
                        constraints={"avoid": {"risk_groups": ["srlg-north"]}})
    found = {p.oms_sequence for p in res.paths}
    assert found == {("oms-south",)}


def test_avoid_all_routes_is_typed_no_solution():
    n = _two_parallel()
    res = compute_paths(n, "A", "B", k=4,
                        constraints={"avoid": {"assets": ["fiber-north", "fiber-south"]}})
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.paths == ()
