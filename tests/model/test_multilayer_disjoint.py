# tests/model/test_multilayer_disjoint.py
"""disjoint_pairs: placement-footprint disjointness over the layered
multilayer graph. A Placement's footprint is computed by flattening its
reused lightpaths to their oms_sequence and unioning with its new runs'
oms_sequence -- never on lightpath identity -- so two different lightpaths
that share a fiber must read as correlated."""
import pytest

from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.multilayer_graph import Placement, NewLightpathRun
from multilayer_optical_mcp.model.multilayer_disjoint import (
    placement_footprint_keys, disjoint_pairs,
)


def _diamond() -> NetworkModel:
    """A<->B direct span (omsAB, lit by lpAB) plus two vertex/fiber-disjoint
    detours A-M1-B and A-M2-B. The detours are only ever referenced via
    NewLightpathRun (never provisioned as lightpaths) so this fixture also
    exercises the new-run half of footprint flattening."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=100e9)]))
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("aAB1", "aAB2", "aAM1a", "aAM1b", "aM1Ba", "aM1Bb",
              "aAM2a", "aAM2b", "aM2Ba", "aM2Bb"):
        n.add_amplifier(Amplifier(a, "advanced_toy", 20.0, 5.5))
    n.add_fiber(Fiber("fAB", "aAB1", "aAB2", 80.0, "SSMF"))
    n.add_fiber(Fiber("fAM1", "aAM1a", "aAM1b", 60.0, "SSMF"))
    n.add_fiber(Fiber("fM1B", "aM1Ba", "aM1Bb", 60.0, "SSMF"))
    n.add_fiber(Fiber("fAM2", "aAM2a", "aAM2b", 60.0, "SSMF"))
    n.add_fiber(Fiber("fM2B", "aM2Ba", "aM2Bb", 60.0, "SSMF"))
    for node in ("A", "B", "M1", "M2"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS("omsAB", "A", "B", ("roadm_A", "aAB1", "fAB", "aAB2")))
    n.add_oms(OMS("omsAM1", "A", "M1", ("roadm_A", "aAM1a", "fAM1", "aAM1b")))
    n.add_oms(OMS("omsM1B", "M1", "B", ("roadm_M1", "aM1Ba", "fM1B", "aM1Bb")))
    n.add_oms(OMS("omsAM2", "A", "M2", ("roadm_A", "aAM2a", "fAM2", "aAM2b")))
    n.add_oms(OMS("omsM2B", "M2", "B", ("roadm_M2", "aM2Ba", "fM2B", "aM2Bb")))
    n.add_lightpath(Lightpath("lpAB", ("omsAB",), "100G", 193.4e12))
    return n


@pytest.fixture
def diamond() -> NetworkModel:
    return _diamond()


def test_reused_and_new_sharing_a_fiber_read_as_correlated(diamond):
    model = diamond
    reused = Placement(reused_lightpaths=("lpAB",), new_lightpaths=(),
                       restored_gbps=100.0, shortfall_gbps=0.0)
    fresh  = Placement(reused_lightpaths=(),
                       new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0, src_node="A", dst_node="B"),),
                       restored_gbps=100.0, shortfall_gbps=0.0)
    ka = placement_footprint_keys(model, reused, basis="physical", level="link")
    kb = placement_footprint_keys(model, fresh,  basis="physical", level="link")
    assert ka & kb        # they share omsAB's fiber -> correlated, NOT disjoint


def test_full_disjoint_pair_found(diamond):
    # Two placements over vertex/fiber-disjoint OMS detours (A-M1-B, A-M2-B)
    # -> a fully disjoint pair exists.
    model = diamond
    pA = Placement(reused_lightpaths=(),
                   new_lightpaths=(NewLightpathRun(("omsAM1", "omsM1B"), 0, "100G",
                                                   15.0, 100.0, src_node="A", dst_node="B"),),
                   restored_gbps=100.0, shortfall_gbps=0.0)
    pB = Placement(reused_lightpaths=(),
                   new_lightpaths=(NewLightpathRun(("omsAM2", "omsM2B"), 1, "100G",
                                                   15.0, 100.0, src_node="A", dst_node="B"),),
                   restored_gbps=100.0, shortfall_gbps=0.0)
    pairs = disjoint_pairs(model, [pA, pB], basis="physical", level="link",
                           best_effort=False, top_n=5)
    assert pairs and pairs[0].disjoint
    assert pairs[0].shared_assets == () and pairs[0].shared_groups == ()
    assert pairs[0].overlap == 0


def test_best_effort_returns_min_overlap_when_none_disjoint(diamond):
    # Both placements ride omsAB (one reused, one a fresh run) -> no disjoint
    # pair exists; best_effort surfaces the (only, fully-overlapping) pair.
    model = diamond
    pA = Placement(reused_lightpaths=("lpAB",), new_lightpaths=(),
                   restored_gbps=100.0, shortfall_gbps=0.0)
    pB = Placement(reused_lightpaths=(),
                   new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                                   src_node="A", dst_node="B"),),
                   restored_gbps=100.0, shortfall_gbps=0.0)
    pairs = disjoint_pairs(model, [pA, pB], basis="physical", level="link",
                           best_effort=True, top_n=5)
    assert pairs and not pairs[0].disjoint and pairs[0].overlap >= 1


def test_no_disjoint_no_best_effort_returns_empty(diamond):
    model = diamond
    pA = Placement(reused_lightpaths=("lpAB",), new_lightpaths=(),
                   restored_gbps=100.0, shortfall_gbps=0.0)
    pB = Placement(reused_lightpaths=(),
                   new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0,
                                                   src_node="A", dst_node="B"),),
                   restored_gbps=100.0, shortfall_gbps=0.0)
    assert disjoint_pairs(model, [pA, pB], basis="physical", level="link",
                          best_effort=False, top_n=5) == []


def test_top_n_truncates_disjoint_pairs_in_generation_order(diamond):
    # Three mutually fiber/vertex-disjoint routes A->B (the direct span lpAB,
    # and the M1/M2 detours): all three pairwise combinations are fully
    # disjoint, so the pairwise scan (i,j) with i<j over [pA, pB, pC] produces
    # exactly 3 pairs in generation order (0,1),(0,2),(1,2) before any
    # truncation.
    model = diamond
    pA = Placement(reused_lightpaths=("lpAB",), new_lightpaths=(),
                   restored_gbps=100.0, shortfall_gbps=0.0)
    pB = Placement(reused_lightpaths=(),
                   new_lightpaths=(NewLightpathRun(("omsAM1", "omsM1B"), 0, "100G",
                                                   15.0, 100.0, src_node="A", dst_node="B"),),
                   restored_gbps=100.0, shortfall_gbps=0.0)
    pC = Placement(reused_lightpaths=(),
                   new_lightpaths=(NewLightpathRun(("omsAM2", "omsM2B"), 1, "100G",
                                                   15.0, 100.0, src_node="A", dst_node="B"),),
                   restored_gbps=100.0, shortfall_gbps=0.0)

    full = disjoint_pairs(model, [pA, pB, pC], basis="physical", level="link",
                          best_effort=False, top_n=10)
    assert len(full) == 3

    truncated = disjoint_pairs(model, [pA, pB, pC], basis="physical", level="link",
                               best_effort=False, top_n=2)
    assert len(truncated) == 2
    assert truncated[0].working is pA and truncated[0].protection is pB
    assert truncated[1].working is pA and truncated[1].protection is pC


def test_hybrid_placement_endpoint_exclusion_needs_explicit_endpoints(diamond):
    """Regression for the audit's Critical cross-implementation-disagreement
    finding. Lights a lightpath on the M1->B leg, then builds a hybrid
    placement whose TRUE physical order is new(A->M1) followed by
    reused(lpM1B) -- the opposite of placement_footprint_keys' unconditional
    reused-then-new concatenation. Without explicit endpoints, the interior
    node M1 is wrongly excluded (as if it were a mandated demand endpoint)
    and the true endpoints A/B are wrongly NOT excluded. With explicit
    endpoints=(src, dst), the result is correct."""
    model = diamond
    model.add_lightpath(Lightpath("lpM1B", ("omsM1B",), "100G", 193.5e12))

    hybrid = Placement(reused_lightpaths=("lpM1B",),
        new_lightpaths=(NewLightpathRun(("omsAM1",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="M1"),),
        restored_gbps=100.0, shortfall_gbps=0.0)

    keys_no_endpoints = placement_footprint_keys(
        model, hybrid, basis="physical", level="node")
    keys_with_endpoints = placement_footprint_keys(
        model, hybrid, basis="physical", level="node", endpoints=("A", "B"))

    # BOTH now include phys keys as a floor (task-6 fix: level="node" is never
    # weaker than level="link"). Empirically-verified exact key sets (see
    # task-6-report.md fix section for the computation):
    #
    # keys_no_endpoints (BROKEN/positional inference): M1 is wrongly treated
    # as a mandated endpoint (first.src == last.dst == M1 under the
    # concatenation's positional order), so it's excluded -- and A/B, the
    # TRUE endpoints, are wrongly kept as node keys and A's roadm survives in
    # phys keys (only roadm_A appears; roadm_M1 does NOT, because M1 is
    # excluded).
    assert keys_no_endpoints == frozenset({
        "node:A", "node:B",
        "phys:aAM1a", "phys:aAM1b", "phys:fAM1", "phys:omsAM1",
        "phys:aM1Ba", "phys:aM1Bb", "phys:fM1B", "phys:omsM1B",
        "phys:roadm_A",
    })
    # Discriminating negatives called out explicitly: the interior transit
    # node M1 (and its ROADM) must NOT appear when endpoints are inferred
    # positionally -- this is the exact bug this test exists to catch.
    assert "node:M1" not in keys_no_endpoints
    assert "phys:roadm_M1" not in keys_no_endpoints

    # keys_with_endpoints (FIXED/explicit endpoints=("A","B")): the TRUE
    # demand endpoints A/B are excluded, leaving the real transit node M1
    # (and its roadm) as the only node-level key -- a second placement
    # sharing M1 would correctly read as correlated.
    assert keys_with_endpoints == frozenset({
        "node:M1",
        "phys:aAM1a", "phys:aAM1b", "phys:fAM1", "phys:omsAM1",
        "phys:aM1Ba", "phys:aM1Bb", "phys:fM1B", "phys:omsM1B",
        "phys:roadm_M1",
    })
    assert "node:M1" in keys_with_endpoints  # interior transit node present
    assert "node:A" not in keys_with_endpoints and "node:B" not in keys_with_endpoints


def test_route_service_and_check_disjointness_agree_on_hybrid_placements(diamond):
    """The cross-implementation property test the audit called for: the
    layered engine's disjoint_pairs (with explicit endpoints) and the flat
    engine's solvers.check_disjointness must agree on the SAME hybrid pair --
    and the scenario must be built so that the FIXED and BROKEN (positional-
    inference) layered verdicts actually differ, or the agreement assertion
    is vacuous (task-5 review finding: the original version of this test
    shared only the ingress ROADM `roadm_A`, which both the fixed and the
    broken code happened to exclude, so `disjoint=True` either way -- the
    assertion passed even with `endpoints` silently ignored).

    Task-6 (48a751d) correctly made level="node" include phys keys as a floor
    (two vertex-disjoint paths are automatically edge-disjoint, so node-level
    must never be weaker than link-level). That fix had a side effect here:
    the ORIGINAL version of this scenario routed `other` back onto the SAME
    OMS `omsM1B` that `hybrid`'s reused lightpath rides, so once phys keys are
    a floor, both the FIXED and BROKEN layered verdicts detect that shared
    span regardless of whether `endpoints` is wired correctly -- masking the
    thing this test exists to catch.

    To restore genuine discrimination, `other` is routed over a test-local
    PARALLEL M1->B OMS (`omsM1B2`, fresh amps + fiber, distinct ids from
    `omsM1B`) instead of the shared one, reached via the extra M2->M1 edge
    (`omsM2M1`). `hybrid` and `other` then share NODE M1 but via physically
    DISTINCT spans -- so the phys-key floor no longer overlaps, and the
    verdict genuinely depends on whether `endpoints` correctly excludes A/B
    (FIXED) vs. wrongly excludes M1 (BROKEN):

    - FIXED (endpoints=("A","B")): hybrid's keys = {node:M1, phys:roadm_M1,
      ...hybrid's own spans}; other's keys = {node:M1, node:M2, phys:roadm_M1,
      ...other's own spans}; shared = {node:M1, phys:roadm_M1} -> NOT
      disjoint.
    - BROKEN (no endpoints, positional inference off the reused-then-new
      concatenation ("omsM1B","omsAM1")): infers M1 as the mandated endpoint
      (first.src == last.dst == M1) and wrongly keeps A/B instead; hybrid's
      keys = {node:A, node:B, ...hybrid's own spans, phys:roadm_A} (no M1/
      roadm_M1 at all); other's keys are unaffected (single ordered
      NewLightpathRun, positional inference happens to work); shared = {} ->
      disjoint.

    These verdicts are opposite, verified empirically against the real code
    (see task-6-report.md fix section for the exact computed key sets) -- so
    this scenario can actually catch `endpoints` being silently dropped. The
    flat engine is then fed the TRUE physical-order OMS sequences (new-then-
    reused for the hybrid leg, matching how a real caller like
    plan.service_oms_sequence would walk the IP path) and must agree with the
    FIXED layered verdict, not the broken one."""
    from multilayer_optical_mcp.model.solvers import check_disjointness
    model = diamond
    model.add_lightpath(Lightpath("lpM1B", ("omsM1B",), "100G", 193.5e12))

    # Extra M2->M1 edge so `other` can transit the diamond's M1 waypoint.
    model.add_amplifier(Amplifier("aM2M1a", "advanced_toy", 20.0, 5.5))
    model.add_amplifier(Amplifier("aM2M1b", "advanced_toy", 20.0, 5.5))
    model.add_fiber(Fiber("fM2M1", "aM2M1a", "aM2M1b", 40.0, "SSMF"))
    model.add_oms(OMS("omsM2M1", "M2", "M1", ("roadm_M2", "aM2M1a", "fM2M1", "aM2M1b")))

    # Test-local PARALLEL M1->B OMS (distinct amps/fiber/id from omsM1B),
    # mirroring how omsM2M1 above is added locally. `other` routes over this
    # instead of the shared omsM1B, so hybrid and other overlap at node M1
    # only -- never at a shared span -- restoring genuine endpoints-kwarg
    # discrimination (Finding 2, task-6 review).
    model.add_amplifier(Amplifier("aM1Ba2", "advanced_toy", 20.0, 5.5))
    model.add_amplifier(Amplifier("aM1Bb2", "advanced_toy", 20.0, 5.5))
    model.add_fiber(Fiber("fM1B2", "aM1Ba2", "aM1Bb2", 60.0, "SSMF"))
    model.add_oms(OMS("omsM1B2", "M1", "B", ("roadm_M1", "aM1Ba2", "fM1B2", "aM1Bb2")))

    hybrid = Placement(reused_lightpaths=("lpM1B",),
        new_lightpaths=(NewLightpathRun(("omsAM1",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="M1"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    other = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAM2", "omsM2M1", "omsM1B2"), 1, "100G",
                                        15.0, 100.0, src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)

    # FIXED layered verdict: explicit endpoints, correctly excludes A/B only.
    fixed_pairs = disjoint_pairs(model, [hybrid, other], basis="physical", level="node",
                                 best_effort=True, top_n=5, endpoints=("A", "B"))
    fixed_disjoint = bool(fixed_pairs) and fixed_pairs[0].disjoint

    # BROKEN layered verdict: no endpoints, positional inference wrongly
    # treats M1 as hybrid's mandated endpoint and excludes it instead of A/B.
    # Since `other` no longer shares a span with `hybrid` (parallel omsM1B2),
    # there is nothing left for the phys-key floor to catch once M1 itself is
    # excluded from hybrid's keys -- so BROKEN genuinely disagrees with FIXED.
    broken_pairs = disjoint_pairs(model, [hybrid, other], basis="physical", level="node",
                                  best_effort=True, top_n=5)
    broken_disjoint = bool(broken_pairs) and broken_pairs[0].disjoint

    assert fixed_disjoint is False   # both routes genuinely cross the M1 waypoint
    assert broken_disjoint is True   # positional inference wrongly excludes M1
    assert fixed_disjoint != broken_disjoint  # endpoints wiring genuinely matters

    # Flat engine, fed the TRUE physical-order OMS sequence for the hybrid
    # leg (new A->M1 run, then the reused M1->B lightpath -- the order a real
    # caller like plan.service_oms_sequence would walk the IP path in), must
    # agree with the FIXED layered verdict, not the broken one.
    flat = check_disjointness(model, ("omsAM1", "omsM1B"), ("omsAM2", "omsM2M1", "omsM1B2"),
                              "physical", "node")

    assert flat.disjoint == fixed_disjoint
    assert flat.disjoint is False
