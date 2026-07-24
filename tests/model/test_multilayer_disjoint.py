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
