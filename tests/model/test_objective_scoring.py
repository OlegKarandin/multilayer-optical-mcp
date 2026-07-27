# tests/model/test_objective_scoring.py
import pytest

from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, ROADM, Router, Service, TransceiverMode,
    Lightpath, IPLink,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.objective import score_candidate, evaluate_objective, score_pair
from multilayer_optical_mcp.model.multilayer_graph import Placement, NewLightpathRun


def _empty_net_model():
    """Empty net (no lightpaths/IP links provisioned yet): a single OMS A->B, two
    routers, one mode ("100G", required_gsnr_db=10.0 -> healthy candidate's
    gsnr_db=15.0 clears it, the low-gsnr candidate's gsnr_db=1.0 does not)."""
    reg = ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=10.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="a1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0,
                      type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="a2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "a1", "fAB", "a2")))
    n.add_router(Router(id="A", site="A"))
    n.add_router(Router(id="B", site="B"))
    n.add_service(Service(id="svc-AB", src_router="A", dst_router="B",
                          demand_gbps=100.0, working_path=()))
    return n


@pytest.fixture
def diamond_service():
    n = _empty_net_model()
    return n, n.get_service("svc-AB")


@pytest.fixture
def diamond_service_lowgsnr():
    n = _empty_net_model()
    return n, n.get_service("svc-AB")


def test_score_candidate_matches_real_commit(diamond_service):
    model, svc = diamond_service   # empty net + one service A->B, demand 100
    cand = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    scored = score_candidate(model, cand, svc)
    # Independently materialize the same candidate on a clone and score directly:
    work = model.clone()
    from multilayer_optical_mcp.model.objective import apply_candidate
    apply_candidate(work, cand, svc)
    assert scored == evaluate_objective(work)   # identical apply path -> identical numbers


def test_margin_negative_candidate_scores_dropped(diamond_service_lowgsnr):
    # run.gsnr_db below the mode's required_gsnr -> seeded margin<0 -> capacity 0
    model, svc = diamond_service_lowgsnr
    cand = Placement((), (NewLightpathRun(("omsAB",), 0, "100G", 1.0, 100.0,
                                          src_node="A", dst_node="B"),), 0.0, 100.0)
    r = score_candidate(model, cand, svc)
    assert r.dropped_traffic > 0.0     # not nominal line rate


def test_score_pair_scratch_ids_do_not_collide_with_committed_protection_leg(diamond_service):
    """Regression: score_pair used allocation.py's real committer prefix
    ("prot") for its throwaway scoring clone, so scoring ANY new
    protection-leg candidate for a service that already has a committed
    protection leg (id lp-prot-{svc.id}-0, allocation.py's _pack naming)
    raised PlanError("already exists") -- blocking route_service's
    documented use for replanning an already-protected service's protection
    leg (CLAUDE.md: reroute_service(which="protection") after a risk group
    reveals correlation)."""
    model, svc = diamond_service   # empty net + one service svc-AB
    # A pre-existing committed protection leg, named exactly as allocation.py's
    # _pack would name a service's first (and only) protection run.
    model.add_lightpath(Lightpath(id="lp-prot-svc-AB-0", oms_sequence=("omsAB",),
                                  mode_id="100G", center_freq_hz=193.4e12))
    model.set_qot_state("lp-prot-svc-AB-0",
                        QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=5.0))
    model.add_ip_link(IPLink(id="ipl-prot-svc-AB-0", a_router="A", z_router="B",
                             lightpath_id="lp-prot-svc-AB-0"))
    model.set_service_protection_path(svc.id, ("ipl-prot-svc-AB-0",))

    working = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    # A NEW protection candidate (not a reuse of the committed leg above) --
    # this is what scoring a replan candidate looks like.
    protection = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 1, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)

    result = score_pair(model, working, protection, svc)   # must not raise
    assert result.transponders > 0.0


def _shrunk_reuse_model() -> NetworkModel:
    """Single OMS A->B, already lit by lpAB and bound to ipAB with spare
    capacity -- a pure-reuse route: no new lightpath needs lighting. Mirrors
    test_route_service.py's _shrunk_model()."""
    reg = ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=10.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="a1", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="a2", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "a1", "fAB", "a2")))
    n.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",),
                              mode_id="100G", center_freq_hz=193.4e12))
    n.set_qot_state("lpAB", QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=5.0))
    n.add_router(Router(id="RA", site="A"))
    n.add_router(Router(id="RB", site="B"))
    n.add_ip_link(IPLink(id="ipAB", a_router="RA", z_router="RB", lightpath_id="lpAB"))
    n.add_service(Service(id="svc", src_router="RA", dst_router="RB",
                          demand_gbps=50.0, working_path=()))
    return n


def test_provision_new_runs_stitches_reused_leg_into_ip_path():
    """A protection leg that grooms onto an already-lit, IP-link-bound survivor
    lightpath (reused_lightpaths non-empty, new_lightpaths empty) must still
    produce a real ip_path segment -- the case provision_new_runs's own
    docstring calls out as 'previously silently dropped' before the
    protection-path lifecycle fix. Also confirms the full downstream stitch:
    apply_op(RerouteService(which="protection")) actually writes the reused
    segment onto Service.protection_path, exactly as allocation.py's _pack
    does for a real solve_allocation commit."""
    from multilayer_optical_mcp.model.objective import provision_new_runs
    from multilayer_optical_mcp.model.plan import apply_op, RerouteService

    n = _shrunk_reuse_model()
    svc = n.get_service("svc")
    work = n.clone()
    placement = Placement(reused_lightpaths=("lpAB",), new_lightpaths=(),
                          restored_gbps=50.0, shortfall_gbps=0.0)

    ip_path = provision_new_runs(work, placement, svc, prefix="prot")

    assert ip_path == ("ipAB",)
    # Pure reuse: no new lightpath or IP link was provisioned.
    assert {lp.id for lp in work.list_lightpaths()} == {"lpAB"}
    assert {l.id for l in work.list_ip_links()} == {"ipAB"}

    # Close the loop: the returned ip_path is exactly what allocation.py's
    # _pack feeds into RerouteService(which="protection") for a real commit.
    apply_op(work, RerouteService(service_id=svc.id, ip_path=ip_path, which="protection"))
    assert work.get_service("svc").protection_path == ("ipAB",)


def test_score_candidate_does_not_collide_with_real_committer_prefix(diamond_service):
    """Regression for the audit's Critical id-namespace finding: score_candidate
    must not raise PlanError when scoring a candidate for a service whose
    existing lightpath was minted by allocation.py's real committer under the
    SAME default 'cand' prefix and the same index-0 id (allocation.py's _pack
    names an unprotected demand's first run exactly 'lp-cand-{demand_id}-0')."""
    model, svc = diamond_service
    # Mint a lightpath the way allocation.py's _pack does for an unprotected
    # demand: prefix="cand" (default), index 0.
    model.add_lightpath(Lightpath(id=f"lp-cand-{svc.id}-0", oms_sequence=("omsAB",),
                                  mode_id="100G", center_freq_hz=193.4e12))
    model.set_qot_state(f"lp-cand-{svc.id}-0",
                        QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=5.0))
    model.add_ip_link(IPLink(id=f"ipl-cand-{svc.id}-0", a_router="A", z_router="B",
                             lightpath_id=f"lp-cand-{svc.id}-0"))

    # A NEW candidate at index 0 (mirrors route_service's real harvest shape --
    # a fresh scoring pass always starts its own new_lightpaths enumeration at
    # index 0, regardless of what's already committed in the model).
    candidate = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 1, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)

    result = score_candidate(model, candidate, svc)   # must not raise
    assert result.transponders > 0.0


def test_apply_candidate_disambiguates_on_id_collision(diamond_service):
    """A real committer re-processing the same service/demand id after its
    prior lightpath was cut must not silently overwrite (or crash on) the
    prior lightpath's id -- _mint_unique must pick a distinct id."""
    from multilayer_optical_mcp.model.objective import apply_candidate
    model, svc = diamond_service
    model.add_lightpath(Lightpath(id=f"lp-cand-{svc.id}-0", oms_sequence=("omsAB",),
                                  mode_id="100G", center_freq_hz=193.4e12))
    model.set_qot_state(f"lp-cand-{svc.id}-0",
                        QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=5.0))
    model.add_ip_link(IPLink(id=f"ipl-cand-{svc.id}-0", a_router="A", z_router="B",
                             lightpath_id=f"lp-cand-{svc.id}-0"))

    candidate = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 1, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)

    apply_candidate(model, candidate, svc)   # must not raise PlanError

    # Both the pre-existing and the newly-minted lightpath must coexist.
    assert f"lp-cand-{svc.id}-0" in model._lightpaths
    assert len(model._lightpaths) == 2
