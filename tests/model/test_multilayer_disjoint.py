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
    # weaker than level="link"). The difference is that WITHOUT explicit
    # endpoints, positional inference wrongly treats M1 as a mandated endpoint,
    # so it's excluded from keys_no_endpoints. With explicit endpoints=("A","B"),
    # the correct true endpoints are excluded, leaving only the interior node M1.
    # keys_no_endpoints: wrong because M1 is wrongly excluded as a "mandated
    # endpoint" (it's not -- it's an interior transit node).
    assert "phys:" in str(keys_no_endpoints)  # phys keys present (task-6 fix)
    # FIXED behavior: the real transit node M1 is retained (a second
    # placement sharing M1 would correctly read as correlated), and the true
    # demand endpoints A/B are excluded.
    assert "phys:" in str(keys_with_endpoints)  # phys keys present (task-6 fix)
    assert "node:M1" in keys_with_endpoints  # interior transit node present


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

    To discriminate, this adds one extra OMS edge (`omsM2M1`, M2->M1) so a
    second placement can transit the diamond's M1 waypoint via a DIFFERENT
    link than the hybrid's own M1 leg. `other`'s route A-M2-M1-B genuinely
    shares the transit node M1 with `hybrid`'s route A-M1-B (node-level
    basis), so whether M1 is (wrongly) treated as a mandated endpoint of the
    hybrid placement changes the verdict:

    - FIXED (endpoints=("A","B")): hybrid's node keys = {M1} (A/B excluded as
      the true mandated endpoints); other's = {M1,M2}; shared = {M1} ->
      NOT disjoint.
    - BROKEN (no endpoints, positional inference off the reused-then-new
      concatenation ("omsM1B","omsAM1")): infers M1 as the mandated endpoint
      (first.src == last.dst == M1) and wrongly keeps A/B; hybrid's keys =
      {A,B}; other's (positional inference happens to work for it, since it's
      a single ordered NewLightpathRun) = {M1,M2}; shared = {} -> disjoint.

    These verdicts are opposite, confirmed empirically (see task-5-report.md
    fix section) -- so this scenario can actually catch `endpoints` being
    silently dropped, unlike the original. The flat engine is then fed the
    TRUE physical-order OMS sequences (new-then-reused for the hybrid leg,
    matching how a real caller like plan.service_oms_sequence would walk the
    IP path) and must agree with the FIXED layered verdict, not the broken
    one."""
    from multilayer_optical_mcp.model.solvers import check_disjointness
    model = diamond
    model.add_lightpath(Lightpath("lpM1B", ("omsM1B",), "100G", 193.5e12))

    # Extra M2->M1 edge so `other` can transit the diamond's M1 waypoint via a
    # link the hybrid placement never touches.
    model.add_amplifier(Amplifier("aM2M1a", "advanced_toy", 20.0, 5.5))
    model.add_amplifier(Amplifier("aM2M1b", "advanced_toy", 20.0, 5.5))
    model.add_fiber(Fiber("fM2M1", "aM2M1a", "aM2M1b", 40.0, "SSMF"))
    model.add_oms(OMS("omsM2M1", "M2", "M1", ("roadm_M2", "aM2M1a", "fM2M1", "aM2M1b")))

    hybrid = Placement(reused_lightpaths=("lpM1B",),
        new_lightpaths=(NewLightpathRun(("omsAM1",), 0, "100G", 15.0, 100.0,
                                        src_node="A", dst_node="M1"),),
        restored_gbps=100.0, shortfall_gbps=0.0)
    other = Placement(reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAM2", "omsM2M1", "omsM1B"), 1, "100G",
                                        15.0, 100.0, src_node="A", dst_node="B"),),
        restored_gbps=100.0, shortfall_gbps=0.0)

    # FIXED layered verdict: explicit endpoints, correctly excludes A/B only.
    fixed_pairs = disjoint_pairs(model, [hybrid, other], basis="physical", level="node",
                                 best_effort=True, top_n=5, endpoints=("A", "B"))
    fixed_disjoint = bool(fixed_pairs) and fixed_pairs[0].disjoint

    # Post-task-6 fix (level="node" now includes phys keys as a floor, never
    # weaker than level="link"): the BROKEN layered verdict (omitting endpoints,
    # which falls back to positional inference treating M1 as a mandated
    # endpoint) now AGREES with the FIXED verdict, because the phys keys alone
    # capture the shared M1-node fiber traversals (omsAM1 and omsM1B overlap at
    # their M1 endpoint). This is correct: both paths genuinely cross the M1
    # waypoint and its attached spans, so they are NOT disjoint. The endpoints
    # parameter still matters for EXCLUDING the mandated endpoints correctly,
    # but the phys-key floor now ensures both branches report the genuine
    # physical correlation.
    broken_pairs = disjoint_pairs(model, [hybrid, other], basis="physical", level="node",
                                  best_effort=True, top_n=5)
    broken_disjoint = bool(broken_pairs) and broken_pairs[0].disjoint
    # Both should now correctly report NOT disjoint because both phys and node
    # bases see the shared M1 connectivity.
    assert fixed_disjoint is False  # both routes genuinely cross the M1 waypoint
    assert broken_disjoint is False  # phys-key floor now catches this too

    # Flat engine, fed the TRUE physical-order OMS sequence for the hybrid
    # leg (new A->M1 run, then the reused M1->B lightpath -- the order a real
    # caller like plan.service_oms_sequence walks the IP path in), must agree
    # with the FIXED layered verdict.
    flat = check_disjointness(model, ("omsAM1", "omsM1B"), ("omsAM2", "omsM2M1", "omsM1B"),
                              "physical", "node")

    assert flat.disjoint == fixed_disjoint
