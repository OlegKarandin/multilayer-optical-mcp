"""FillPolicy: the acceptance-probe reference-loading policy.

ACTUAL probes against the channels lit at probe time — order-dependent,
optimistic. FULL (default) probes against a fully-loaded comb so the delivered
mode is chosen to stay feasible as the network fills: order-independent and
margin-stable. FULL is acceptance-time only; the operating recompute is
untouched (see the plan's acceptance-only decision).
"""
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
