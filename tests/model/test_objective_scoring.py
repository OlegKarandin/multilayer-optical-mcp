# tests/model/test_objective_scoring.py
import pytest

from multilayer_optical_mcp.model.assets import FiberType, Amplifier, Fiber, OMS, ROADM, TransceiverMode, Lightpath
from multilayer_optical_mcp.model.ip_assets import Router, Service, IPLink
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


def test_score_pair_counts_both_legs_margin_when_working_and_protection_share_an_oms(
        diamond_service):
    """Final-review Fix round 2 residual: score_pair calls apply_candidate
    (working leg) and provision_new_runs (protection leg) as two SEPARATE
    calls on the same throwaway clone. Each call now re-seeds its OWN runs
    before returning (Fix 1), but that only makes each call internally
    consistent -- it does not protect one call's already-returned seed from
    the OTHER call's later provisioning. When working and protection share a
    physical OMS at different wavelengths (legal: same fiber, no NLI/SRLG
    conflict at the frequency level; reachable under basis="physical" +
    best_effort=True -- CLAUDE.md's degraded-restoration case -- or under any
    relaxed basis like srlg/risk_group/union), provisioning the protection
    leg invalidates the working leg's seed via Task 4's cross-lightpath
    invalidation, and evaluate_objective's total_margin loop silently skips
    the wiped lightpath. This corrupts both total_margin (undercounted) and
    max_util (a fully-loaded working-leg IP link reads 0.0, since its bound
    lightpath has no QoT to derive capacity from) -- exactly the "confident
    wrong number" class CLAUDE.md's derived-capacity/margin-gate rules exist
    to prevent, on route_service's protected-menu ranking."""
    model, svc = diamond_service   # empty net + one service svc-AB, omsAB A->B
    # Working leg: omsAB lam 0. Protection leg: omsAB lam 1 -- same physical
    # OMS as the working leg, different wavelength (co-located, not a reuse).
    working = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    protection = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 1, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)

    scored = score_pair(model, working, protection, svc)

    # Ground truth: materialize the same pair on a fresh clone the same way
    # score_pair does, then sum margin_db over every lightpath that still has
    # a recorded QoT state -- evaluate_objective's own total_margin loop.
    work = model.clone()
    from multilayer_optical_mcp.model.objective import apply_candidate, provision_new_runs
    seeded_working = apply_candidate(work, working, work.get_service(svc.id),
                                     prefix="score-work")
    _ip_path, seeded_protection = provision_new_runs(
        work, protection, work.get_service(svc.id), prefix="score-prot")
    assert len(seeded_working) == 1 and len(seeded_protection) == 1
    for lp_id, state in (*seeded_working, *seeded_protection):
        work.set_qot_state(lp_id, state)

    direct_sum = sum(work.get_qot_state(lp.id).margin_db for lp in work.list_lightpaths())
    assert scored.total_margin == pytest.approx(direct_sum)

    # The specific claim under test: BOTH legs' margins counted, and the
    # working leg's IP link is correctly seen as loaded (not silently 0.0).
    seeded_margins = [work.get_qot_state(lp_id).margin_db
                      for lp_id, _st in (*seeded_working, *seeded_protection)]
    assert all(m > 0 for m in seeded_margins), "expected both legs margin-positive"
    assert scored.total_margin >= max(seeded_margins), (
        "total_margin dropped to a single leg's contribution -- the other "
        "leg's QoT was silently wiped and skipped")
    assert scored.max_util > 0.0, (
        "working leg's IP link read as unloaded -- its lightpath's QoT (and "
        "therefore derived capacity) was wiped by the protection leg's "
        "later provisioning")


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

    ip_path, _seeded = provision_new_runs(work, placement, svc, prefix="prot")

    assert ip_path == ("ipAB",)
    # Pure reuse: no new lightpath or IP link was provisioned.
    assert {lp.id for lp in work.list_lightpaths()} == {"lpAB"}
    assert {ip_link.id for ip_link in work.list_ip_links()} == {"ipAB"}

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


def test_score_candidate_counts_both_colocated_lightpaths_margin():
    """Final-review Fix 1: score_candidate/score_pair call apply_candidate/
    provision_new_runs directly on a throwaway scoring clone and never see
    _pack's external re-seed accumulation, so a co-located-siblings placement
    (two new lightpath runs sharing one physical OMS -- Task 6's confirmed-
    real scenario) used to have its SECOND run's provisioning wipe the
    FIRST's just-seeded QoT (Task 4's cross-lightpath invalidation), and
    evaluate_objective's total_margin loop silently skips any lightpath with
    no recorded QoT state -- systematically under-counting exactly the
    co-located placements Task 6 made a common, correctly-preferred outcome.
    This under-ranked those candidates in route_service's cost_vector["scalar"]
    ordering, the opposite of Task 6's intent.

    Now that apply_candidate re-applies its own seeds before returning (Fix
    1), score_candidate's clone has BOTH lightpaths' QoT correct, and
    total_margin equals the direct sum of both -- no silent skip."""
    from multilayer_optical_mcp.model.ip_assets import Service
    from multilayer_optical_mcp.model.multilayer_graph import build_layered_graph
    from multilayer_optical_mcp.model.allocation import make_adapter_evaluator
    from multilayer_optical_mcp.model.qot_results import QoTResultStore
    from multilayer_optical_mcp.model.spectrum import FillPolicy
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

    svc = Service(id="svc-a", src_router="router_C", dst_router="router_Z",
                 demand_gbps=100.0, working_path=())
    n.add_service(svc)

    scored = score_candidate(n, target, svc)

    # Independently materialize the same candidate on a fresh clone (n
    # already carries svc-a, added above) via apply_candidate's now-self-
    # corrected seeds, then sum margin_db over EVERY lightpath that still has
    # a recorded QoT state -- exactly evaluate_objective's own total_margin
    # loop (model/objective.py). This is the ground truth score_candidate's
    # total_margin must match; identical to test_score_candidate_matches_real_
    # commit's pattern above, but on a fixture with co-located siblings.
    #
    # Task E: _shared_oms_mesh_model also seeds "lp-bg-CM" on oms_C_M (margin
    # 3.0) -- a genuine BYSTANDER (not one of target's own new runs) that
    # shares an OMS with them. score_candidate now snapshots and restores
    # every pre-existing lightpath's QoT around provisioning (see
    # _snapshot_lightpath_qot/_restore_bystander_qot in objective.py), so the
    # ground truth here must do the same to stay an apples-to-apples
    # comparison -- otherwise this hand-rolled reimplementation just
    # reintroduces the bystander-wipe bug score_candidate itself no longer has.
    work = n.clone()
    from multilayer_optical_mcp.model.objective import (
        apply_candidate, _snapshot_lightpath_qot, _restore_bystander_qot,
    )
    bystanders = _snapshot_lightpath_qot(work)
    seeded = apply_candidate(work, target, work.get_service("svc-a"))
    _restore_bystander_qot(work, bystanders)
    assert len(seeded) == 2

    def _has_qot(lp_id):
        try:
            work.get_qot_state(lp_id)
            return True
        except LookupError:
            return False

    direct_sum = sum(work.get_qot_state(lp.id).margin_db
                     for lp in work.list_lightpaths() if _has_qot(lp.id))
    assert scored.total_margin == pytest.approx(direct_sum)

    # The specific claim under test: BOTH co-located siblings' margins (not
    # just one) are counted. Pre-Fix-1, apply_candidate would have left one
    # of the two `seeded` lightpaths with no QoT state (see
    # test_apply_candidate_self_corrects_seed_wiped_by_sibling_run), so
    # total_margin would have silently dropped its (positive) contribution --
    # reading strictly lower than the true value. Confirm both margins are
    # positive contributors, and that total_margin exceeds what it would be
    # with either single one omitted.
    seeded_margins = [work.get_qot_state(lp_id).margin_db for lp_id, _st in seeded]
    assert all(m > 0 for m in seeded_margins), "expected both siblings margin-positive"
    other_lps_sum = direct_sum - sum(seeded_margins)
    for omit in seeded_margins:
        under_count = other_lps_sum + (sum(seeded_margins) - omit)
        assert scored.total_margin > under_count, (
            "total_margin must reflect BOTH co-located lightpaths' margins, "
            "not just one (the pre-Fix-1 under-count)")


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


# --------------------------------------------------------------- Task E: bystander QoT
#
# Task E: score_candidate/score_pair provision new lightpaths on a THROWAWAY
# clone and re-seed their OWN just-provisioned QoT (Fix 1/round 2 above), but
# neither protects a PRE-EXISTING ("bystander") lightpath that is not part of
# the candidate itself yet happens to share an OMS with one of the candidate's
# new runs. NetworkModel._invalidate_qot_sharing_oms wipes that bystander's
# QoT too, and nothing re-applies it. evaluate_objective's total_margin loop
# silently skips a QoT-less lightpath, and ip_link_capacity_gbps raises
# LookupError for one (read by simulate_ip_routing as "unknown", not
# congested/down) -- so the bystander's margin vanishes from the score, and if
# the bystander was actually congested, the evidence of that congestion goes
# invisible right along with it.

def test_score_candidate_preserves_bystander_margin_sharing_oms(diamond_service):
    """A bare pre-existing lightpath sharing omsAB with the candidate's new
    run must keep contributing its margin to total_margin. Candidate's own
    margin: gsnr 15.0 - required 10.0 = 5.0. Bystander's seeded margin: 10.0.
    Pre-fix, provisioning the candidate's new run on omsAB wipes the
    bystander's QoT (shared OMS -> _invalidate_qot_sharing_oms),
    evaluate_objective's total_margin loop silently skips it, and
    total_margin reads only 5.0. Post-fix it reads 15.0."""
    model, svc = diamond_service   # empty net + svc-AB, omsAB A->B
    model.add_lightpath(Lightpath(id="lp-bystander", oms_sequence=("omsAB",),
                                  mode_id="100G", center_freq_hz=193.4e12))
    model.set_qot_state("lp-bystander",
                        QoTState(gsnr_db=20.0, osnr_db=30.0, margin_db=10.0))

    cand = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 1, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)

    result = score_candidate(model, cand, svc)

    assert result.total_margin == pytest.approx(15.0), (
        "bystander's margin (10.0) must be preserved alongside the "
        "candidate's own (5.0), not silently wiped and skipped")


def test_score_candidate_preserves_bystander_congestion_evidence_sharing_oms(
        diamond_service):
    """A pre-existing, ALREADY-OVERSUBSCRIBED bystander service (real
    congestion: demand 150 Gbps over a 100 Gbps lightpath, 50 Gbps overflow)
    sharing omsAB with the scored candidate's new run must still read as
    congested in the returned ObjectiveResult. Pre-fix, provisioning the
    candidate's run wipes the bystander lightpath's QoT, its IP link's
    capacity becomes "unknown" (LookupError), simulate_ip_routing treats an
    unknown link as neither congested nor down, and the 50 Gbps overflow +
    the 1.5x utilization both silently vanish from the score -- exactly the
    "erases the evidence of a real problem" failure mode. Post-fix, the
    bystander's original QoT (hence its true congestion) is restored before
    evaluate_objective reads it."""
    model, svc = diamond_service   # empty net + svc-AB (demand 100), omsAB A->B
    model.add_lightpath(Lightpath(id="lp-bystander", oms_sequence=("omsAB",),
                                  mode_id="100G", center_freq_hz=193.4e12))
    model.set_qot_state("lp-bystander",
                        QoTState(gsnr_db=20.0, osnr_db=30.0, margin_db=10.0))
    model.add_ip_link(IPLink(id="ipl-bystander", a_router="A", z_router="B",
                             lightpath_id="lp-bystander"))
    model.add_service(Service(id="svc-bystander", src_router="A", dst_router="B",
                              demand_gbps=150.0, working_path=("ipl-bystander",)))

    cand = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 1, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)

    result = score_candidate(model, cand, svc)

    # 100G capacity on lp-bystander, 150 Gbps offered -> 50 Gbps overflow,
    # 1.5x utilization. svc-AB's own new link (100/100) is exactly at 1.0x,
    # not congested, contributing 0 overflow -- so any surviving overflow/
    # utilization signal traces only to the bystander.
    assert result.dropped_traffic == pytest.approx(50.0), (
        "bystander's real overflow (50 Gbps) must survive scoring, not read "
        "as 0.0 because its QoT (and therefore capacity) was wiped")
    assert result.max_util == pytest.approx(1.5), (
        "bystander's true 1.5x utilization must survive scoring, not read "
        "as unknown/excluded because its QoT was wiped")


def test_score_pair_preserves_bystander_margin_sharing_oms(diamond_service):
    """Same protection as score_candidate's bystander-margin test, but for
    score_pair: a pre-existing lightpath that is neither the working nor the
    protection leg being scored, sharing omsAB with BOTH legs' new runs, must
    keep contributing its margin. Working margin: 5.0 (gsnr 15 - required
    10). Protection margin: 5.0. Bystander margin: 10.0. Pre-fix, either
    leg's provisioning wipes the bystander's QoT and evaluate_objective's
    total_margin loop silently skips it -- total_margin would read 10.0
    (both legs only). Post-fix it reads 20.0 (both legs + bystander)."""
    model, svc = diamond_service   # empty net + svc-AB, omsAB A->B
    model.add_lightpath(Lightpath(id="lp-bystander", oms_sequence=("omsAB",),
                                  mode_id="100G", center_freq_hz=193.4e12))
    model.set_qot_state("lp-bystander",
                        QoTState(gsnr_db=20.0, osnr_db=30.0, margin_db=10.0))

    working = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    protection = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 1, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)

    result = score_pair(model, working, protection, svc)

    assert result.total_margin == pytest.approx(20.0), (
        "bystander's margin (10.0) must be preserved alongside both legs' "
        "own (5.0 + 5.0), not silently wiped and skipped")


# --------------------------------------------------------------- Prefix assertion tests
#

def test_apply_candidate_accepts_real_committer_prefix_cand(diamond_service):
    """apply_candidate with prefix="cand" (real committer reserved value)
    must not raise."""
    model, svc = diamond_service
    candidate = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    from multilayer_optical_mcp.model.objective import apply_candidate
    work = model.clone()
    # Must not raise ValueError for reserved prefix "cand"
    apply_candidate(work, candidate, svc, prefix="cand")


def test_apply_candidate_accepts_default_prefix(diamond_service):
    """apply_candidate with default prefix (="cand") must not raise."""
    model, svc = diamond_service
    candidate = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    from multilayer_optical_mcp.model.objective import apply_candidate
    work = model.clone()
    # Must not raise ValueError for default prefix (="cand")
    apply_candidate(work, candidate, svc)


def test_provision_new_runs_accepts_reserved_prefix_prot(diamond_service):
    """provision_new_runs with prefix="prot" (real committer reserved value)
    must not raise."""
    model, svc = diamond_service
    placement = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    from multilayer_optical_mcp.model.objective import provision_new_runs
    work = model.clone()
    # Must not raise ValueError for reserved prefix "prot"
    provision_new_runs(work, placement, svc, prefix="prot")


def test_apply_candidate_rejects_prefix_colliding_with_reserved(diamond_service):
    """apply_candidate with prefix="cand-explore" (collides with reserved
    "cand") must raise ValueError."""
    model, svc = diamond_service
    candidate = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    from multilayer_optical_mcp.model.objective import apply_candidate
    work = model.clone()
    with pytest.raises(ValueError, match="collides with reserved committer"):
        apply_candidate(work, candidate, svc, prefix="cand-explore")


def test_provision_new_runs_rejects_prefix_colliding_with_reserved(diamond_service):
    """provision_new_runs with prefix="prot2" (collides with reserved "prot")
    must raise ValueError."""
    model, svc = diamond_service
    placement = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    from multilayer_optical_mcp.model.objective import provision_new_runs
    work = model.clone()
    with pytest.raises(ValueError, match="collides with reserved committer"):
        provision_new_runs(work, placement, svc, prefix="prot2")


def test_apply_candidate_accepts_score_prefix_no_collision(diamond_service):
    """apply_candidate with prefix="score-cand" (scoring prefix, no collision
    with reserved namespace) must not raise."""
    model, svc = diamond_service
    candidate = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    from multilayer_optical_mcp.model.objective import apply_candidate
    work = model.clone()
    # Must not raise ValueError for non-reserved, non-colliding prefix
    apply_candidate(work, candidate, svc, prefix="score-cand")
