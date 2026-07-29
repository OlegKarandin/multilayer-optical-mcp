"""FillPolicy: the acceptance-probe reference-loading policy.

ACTUAL probes against the channels lit at probe time — order-dependent,
optimistic. FULL (default) probes against a fully-loaded comb so the delivered
mode is chosen to stay feasible as the network fills: order-independent and
margin-stable. FULL is acceptance-time only; the operating recompute is
untouched (see the plan's acceptance-only decision).
"""
import pytest

from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, TransceiverMode, Router,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.spectrum import SpectrumGrid, FillPolicy
from multilayer_optical_mcp.model.allocation import (
    _build_loading, solve_rsa, solve_allocation,
)


# --------------------------------------------------------------- _build_loading unit

def test_build_loading_actual_includes_only_occupied_neighbors():
    """ACTUAL: probe + exactly the slots already lit on the path's OMS."""
    grid = SpectrumGrid.default()
    state = {"oms1": (1 << 5) | (1 << 7)}          # slots 5 and 7 lit
    ls = _build_loading(grid, state, ("oms1",), probe_slot=10, ref_mode_id="ref",
                        fill_policy=FillPolicy.ACTUAL)
    slots = sorted(grid.slot_of(c.center_freq_hz) for c in ls.channels)
    assert slots == [5, 7, 10]                     # probe + 2 occupied neighbors


def test_build_loading_full_fills_all_non_probe_slots():
    """FULL: probe + every other grid slot, regardless of occupancy."""
    grid = SpectrumGrid.default()
    state = {"oms1": (1 << 5)}                      # occupancy is ignored under FULL
    ls = _build_loading(grid, state, ("oms1",), probe_slot=10, ref_mode_id="ref",
                        fill_policy=FillPolicy.FULL)
    slots = sorted(grid.slot_of(c.center_freq_hz) for c in ls.channels)
    assert slots == list(range(grid.num_slots))    # every slot, probe included
    assert ls.channels[0].center_freq_hz == grid.freq(10)  # probe still first


# --------------------------------------------------------------- behavior via solvers

class ChannelCountQot:
    """GSNR falls as the interferer count rises: 20 dB with one carrier, minus
    0.2 dB per extra channel. Lets FULL (dense comb) select a lower mode than
    ACTUAL (sparse comb) on the very same route."""
    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        n = len(loading.channels)
        return QoTState(gsnr_db=20.0 - 0.2 * (n - 1), osnr_db=30.0, margin_db=0.0)


def _modes() -> ModeRegistry:
    # 400G needs 15 dB, 100G needs 5 dB.
    return ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=5.0,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
        TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=15.0,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    ])


def _one_route() -> NetworkModel:
    n = NetworkModel(modes=_modes())
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("a1", "a2"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber("f1", "a1", "a2", 80.0, "SSMF"))
    for node in ("A", "Z"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS("oms1", "A", "Z", ("roadm_A", "a1", "f1", "a2")))
    n.add_router(Router(id="r_A", site="A"))
    n.add_router(Router(id="r_Z", site="Z"))
    return n


def test_solve_rsa_full_downshifts_relative_to_actual():
    """Same greenfield route: ACTUAL sees one carrier (400G feasible); FULL sees
    the full comb (only 100G feasible) — the margin-stability behavior."""
    qot = ChannelCountQot()
    demand = [{"id": "d1", "src": "A", "dst": "Z"}]

    actual = solve_rsa(_one_route(), qot, demand,
                      fill_policy=FillPolicy.ACTUAL)          # pinned: ACTUAL-specific
    full = solve_rsa(_one_route(), qot, demand, fill_policy=FillPolicy.FULL)

    assert actual.placements[0].working.mode_id == "400G"
    assert full.placements[0].working.mode_id == "100G"


def test_solve_rsa_defaults_to_full_downshift():
    """With the flipped default, solve_rsa (no fill_policy) now behaves as FULL:
    the dense comb forces the margin-stable lower mode, matching an explicit FULL."""
    qot = ChannelCountQot()
    demand = [{"id": "d1", "src": "A", "dst": "Z"}]
    default = solve_rsa(_one_route(), qot, demand)                       # no policy arg
    explicit_full = solve_rsa(_one_route(), qot, demand, fill_policy=FillPolicy.FULL)
    assert default.placements[0].working.mode_id == explicit_full.placements[0].working.mode_id
    assert default.placements[0].working.mode_id == "100G"


def test_solve_allocation_full_is_order_independent():
    """FULL's canonical loading makes the delivered mode independent of placement
    order: every demand probes against the same full comb."""
    qot = ChannelCountQot()
    ds = [{"id": "d1", "src": "A", "dst": "Z", "demand_gbps": 100.0},
          {"id": "d2", "src": "A", "dst": "Z", "demand_gbps": 100.0}]

    def modes_by_demand(weights):
        res = solve_allocation(_one_route(), qot, ds, spare_inventory={"A": 9, "Z": 9},
                               weights=weights, fill_policy=FillPolicy.FULL)
        assert res.status is SolverStatus.SOLUTION
        return {p.demand_id: r.mode_id for p in res.placements for r in p.new_lightpaths}

    order1 = modes_by_demand({"d1": 10.0, "d2": 1.0})
    order2 = modes_by_demand({"d1": 1.0, "d2": 10.0})
    assert order1 == order2                                     # order-independent
    assert set(order1.values()) == {"100G"}                    # FULL comb -> 100G


# ----------------------------------------------- S7-10: co-located new runs (real adapter)
#
# Task 6 investigation: the multilayer_graph module docstring's Stage 7
# assumptions flagged an "assumed OMS-disjoint" precondition for new-lightpath
# runs within one placement -- unproven, and audited as "plausible/theoretical,
# not concretely reproduced". This section builds the mesh fixture the audit
# didn't have time to build (WLIN/WLOUT+EXPRESS lets a later run re-enter a
# physical span an earlier sibling in the SAME placement already used, at a
# different wavelength) and drives it with the REAL GNPy adapter under
# FillPolicy.ACTUAL. Confirmed real: ~0.03 dB optimism on an 800 km/10-span
# shared OMS (place_demands QoT'd each run against the committed spectrum
# snapshot only, missing the uncommitted sibling's channel). Fixed in
# multilayer_graph.place_demands (see its S7-10 comment); this test is the
# regression.

from multilayer_optical_mcp.model.assets import Lightpath
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.multilayer_graph import build_layered_graph, place_demands
from multilayer_optical_mcp.model.allocation import make_adapter_evaluator, _best_feasible_mode
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState

_S710_MODE = "400G@7.1dB"


def _s710_modes() -> ModeRegistry:
    return ModeRegistry([
        TransceiverMode(id=_S710_MODE, bitrate_gbps=400.0, required_gsnr_db=7.1,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9, roll_off=0.15),
    ])


def _shared_oms_mesh_model():
    """C -> M -> Z is the direct route; C -> M -> D -> C -> M -> Z is a
    detour that legitimately re-enters oms_C_M a second time (via EXPRESS at
    C, on a different wavelength) after grooming through D -- the mechanism
    that lets two new-lightpath runs in ONE placement share a physical OMS.
    oms_C_M is 800 km/10 spans (NLI-sensitive: ~0.03-0.08 dB per extra
    co-propagating channel, confirmed against the real adapter -- a single
    80 km span measured near-zero sensitivity, per the Task 5 finding this
    investigation was warned about). J/K is a disconnected throwaway edge;
    background lightpaths on it and on oms_C_M itself force >=2 wavelength
    layers and a non-empty ACTUAL occupancy, both needed so the comparison
    below isn't accidentally masked by compute_qot's auto-pad-to-2-channel
    dummy (see the comment at the assertion site)."""
    graph = {
        "nodes": [{"id": x} for x in ("C", "M", "D", "Z", "J", "K")],
        "edges": [
            {"src": "C", "dst": "M", "length_km": 800.0},
            {"src": "M", "dst": "D", "length_km": 80.0},
            {"src": "D", "dst": "C", "length_km": 80.0},
            {"src": "M", "dst": "Z", "length_km": 80.0},
            {"src": "J", "dst": "K", "length_km": 80.0},
        ],
    }
    n = model_from_abstract_graph(graph, modes=_s710_modes())
    grid = SpectrumGrid.default()
    for slot in range(3):
        n.add_lightpath(Lightpath(f"lp-junk-{slot}", ("oms_J_K",), _S710_MODE, grid.freq(slot)))
        n.set_qot_state(f"lp-junk-{slot}", QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=3.0))
    n.add_lightpath(Lightpath("lp-bg-CM", ("oms_C_M",), _S710_MODE, grid.freq(6)))
    n.set_qot_state("lp-bg-CM", QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=3.0))
    return n, grid


def _runs_using(placement, oms_id):
    return [r for r in placement.new_lightpaths if oms_id in r.oms_sequence]


def test_place_demands_finds_two_new_runs_sharing_an_oms():
    """Mechanism check: FillPolicy.ACTUAL isn't the only thing under test --
    first confirm place_demands' own path search actually produces a
    placement with two DISTINCT new-lightpath runs both touching oms_C_M
    (the S7-10 docstring's "assumed OMS-disjoint" precondition is false)."""
    n, grid = _shared_oms_mesh_model()
    qot = make_adapter_evaluator(n, QoTResultStore())
    g = build_layered_graph(n, grid=grid)
    res = place_demands(n, g, qot, src="C", dst="Z", demand_gbps=100.0,
                        policy="new_only", k=20, grid=grid,
                        fill_policy=FillPolicy.ACTUAL)
    assert any(len(_runs_using(p, "oms_C_M")) == 2 for p in res), (
        "expected a placement with two new runs sharing oms_C_M")


def test_actual_colocated_runs_include_each_others_channel():
    """The fix: each co-located run's loading must include its sibling's
    channel on the shared OMS before either is QoT'd under ACTUAL. Asserts
    the GSNR place_demands actually delivers matches a reference computed
    with both channels present in the loading from the start, and that this
    is not a vacuous check -- the pre-fix ("alone") value is measurably
    higher (optimistic) than the reference."""
    n, grid = _shared_oms_mesh_model()
    store = QoTResultStore()
    qot = make_adapter_evaluator(n, store)
    g = build_layered_graph(n, grid=grid)
    res = place_demands(n, g, qot, src="C", dst="Z", demand_gbps=100.0,
                        policy="new_only", k=20, grid=grid,
                        fill_policy=FillPolicy.ACTUAL)

    target = next((p for p in res if len(_runs_using(p, "oms_C_M")) == 2), None)
    assert target is not None, "expected a placement with two new runs sharing oms_C_M"
    run_a, run_b = _runs_using(target, "oms_C_M")

    ref_mode = n.modes.list()[0].id
    from multilayer_optical_mcp.model.spectrum import build_spectrum_state
    spectrum = build_spectrum_state(n, grid)
    # run_a's loading as place_demands built it BEFORE the sibling-channel fix
    # (each run QoT'd against the committed spectrum snapshot alone -- what
    # was optimistic under ACTUAL).
    loading_alone = _build_loading(grid, spectrum, run_a.oms_sequence, run_a.lam,
                                   ref_mode, FillPolicy.ACTUAL)
    # The correct comb: run_a's own loading plus run_b's channel on the shared
    # OMS, reflecting that both genuinely co-propagate on oms_C_M once this
    # placement is accepted as a whole.
    sibling_channel = Channel(grid.freq(run_b.lam), grid.spacing_hz, None, ref_mode)
    loading_correct = LoadingState(loading_alone.channels + (sibling_channel,))

    _, gsnr_alone = _best_feasible_mode(n, qot, run_a.oms_sequence, loading_alone, ref_mode)
    _, gsnr_correct = _best_feasible_mode(n, qot, run_a.oms_sequence, loading_correct, ref_mode)

    # The fix: place_demands now delivers the correct (sibling-inclusive) GSNR.
    assert run_a.gsnr_db == pytest.approx(gsnr_correct, abs=1e-9)
    # Not vacuous: the un-fixed ("alone") value is measurably optimistic --
    # confirms this fixture actually exercises the bug the fix addresses.
    assert gsnr_alone > gsnr_correct + 0.01
