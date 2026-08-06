"""placement_common: the three helpers previously duplicated (or, for
_forbidden_assets/_lever, defined-but-only-imported-elsewhere) across
allocation.py, route_service.py, and restoration.py."""

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
    bad = _forbidden_assets(n, {"assets": ["fZZ"], "srlgs": ["srlg1"],
                               "risk_groups": ["rg1"]})
    assert bad == frozenset({"fZZ", "fAB", "fBC", "fXY"})


def test_forbidden_assets_risk_groups_key_no_longer_matches_srlg():
    """S6-7 fix: an SRLG id under avoid.risk_groups must not expand — srlgs and
    risk_groups are now two distinct namespaces."""
    n = _model()
    n.add_srlg(SRLG(id="srlg1", asset_ids=("fAB", "fBC")))
    bad = _forbidden_assets(n, {"risk_groups": ["srlg1"]})
    assert bad == frozenset()


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


from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, TransceiverMode,
)
from multilayer_optical_mcp.model.multilayer_graph import build_layered_graph
from multilayer_optical_mcp.model.placement_common import _harvest_placements


def _spanned_model() -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=100e9)]))
    n.register_fiber_type(FiberType("SSMF", 0.2))
    n.add_amplifier(Amplifier("a1", "advanced_toy", 20.0, 5.5))
    n.add_fiber(Fiber("fAB", "a1", "a2", 80.0, "SSMF"))
    n.add_amplifier(Amplifier("a2", "advanced_toy", 20.0, 5.5))
    n.add_roadm(ROADM(id="roadm_A"))
    n.add_roadm(ROADM(id="roadm_B"))
    n.add_oms(OMS("omsAB", "A", "B", ("roadm_A", "a1", "fAB", "a2")))
    return n


def _fake_qot(gsnr_db=22.0):
    from multilayer_optical_mcp.model.qot import QoTState

    def _eval(*, oms_sequence, direction, mode_id, loading):
        return QoTState(gsnr_db=gsnr_db, osnr_db=gsnr_db + 2.0,
                        margin_db=gsnr_db - 12.0)
    return _eval


def test_harvest_placements_dedupes_across_groom_or_new_and_new_only():
    model = _spanned_model()
    g = build_layered_graph(model)
    out = _harvest_placements(model, _fake_qot(), g, "A", "B", 50.0, k=8)
    # Both policies discover the same single new-lightpath run over the only
    # OMS (no existing lightpath to groom onto) -> dedup collapses it to one.
    seen = {(p.reused_lightpaths, tuple(r.oms_sequence for r in p.new_lightpaths))
            for p in out}
    assert len(out) == len(seen)      # no duplicate route identity survives
    assert len(out) >= 1
