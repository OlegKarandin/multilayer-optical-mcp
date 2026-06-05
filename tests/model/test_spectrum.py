"""Spectrum state: fixed grid + per-OMS slot bitmasks (efficient bitwise RSA).

Occupancy is STORED as one integer bit-vector per OMS; feasibility along a path
is a bitwise OR of the path's OMS masks. (This is not the IP-capacity
'derived, never stored' rule — that is about capacity = f(mode), not spectrum.)
"""
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.spectrum import (
    SpectrumGrid, build_spectrum_state, free_slots_along, first_fit_slot,
    reserve, check_spectrum_feasibility, FeasibilityResult,
)


def _grid() -> SpectrumGrid:
    return SpectrumGrid.default()  # 48 slots @ 100 GHz, slot 20 == 193.4 THz


def _model(*, lit_slot=None) -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=7.1,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for a in ("aN1", "aN2", "aS1", "aS2"):
        n.add_amplifier(Amplifier(id=a, type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fN", a_end="aN1", z_end="aN2", length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fS", a_end="aS1", z_end="aS2", length_km=80.0, type_variety="SSMF"))
    n.add_oms(OMS(id="oms-north", src_node_id="A", dst_node_id="Z", elements=("aN1", "fN", "aN2")))
    n.add_oms(OMS(id="oms-south", src_node_id="A", dst_node_id="Z", elements=("aS1", "fS", "aS2")))
    if lit_slot is not None:
        grid = _grid()
        n.add_lightpath(Lightpath(id="lp-north", oms_sequence=("oms-north",),
                                  mode_id="400G", center_freq_hz=grid.freq(lit_slot)))
    return n


def test_grid_freq_slot_roundtrip():
    g = _grid()
    assert g.num_slots == 48
    assert g.slot_of(g.freq(20)) == 20
    assert abs(g.freq(20) - 193.4e12) < 1.0
    # Every slot lies inside gnpy's C-band (191.4–196.1 THz).
    assert 191.3e12 <= g.freq(0) and g.freq(g.num_slots - 1) <= 196.2e12


def test_build_state_reflects_lightpaths():
    g = _grid()
    n = _model(lit_slot=10)
    state = build_spectrum_state(n, g)
    assert state["oms-north"] & (1 << 10)        # slot 10 occupied on north
    assert not (state.get("oms-south", 0) & (1 << 10))  # south is free


def test_first_fit_skips_occupied_slot():
    g = _grid()
    n = _model(lit_slot=0)
    state = build_spectrum_state(n, g)
    # Slot 0 occupied on north -> first fit on north is slot 1.
    assert first_fit_slot(state, ("oms-north",), g) == 1
    # South untouched -> first fit is slot 0.
    assert first_fit_slot(state, ("oms-south",), g) == 0


def test_free_slots_is_bitwise_or_along_path():
    g = _grid()
    n = _model(lit_slot=3)
    state = build_spectrum_state(n, g)
    # A path crossing both OMS sees slot 3 occupied (from north).
    free = free_slots_along(state, ("oms-north", "oms-south"), g)
    assert not (free & (1 << 3))
    assert free & (1 << 4)


def test_reserve_then_blocks_that_slot():
    g = _grid()
    n = _model()
    state = build_spectrum_state(n, g)
    reserve(state, ("oms-north",), 5)
    assert first_fit_slot(state, ("oms-north",), g) == 0
    fr = check_spectrum_feasibility(n, ("oms-north",), 5, grid=g, extra_state=state)
    assert fr.feasible is False
    assert any(c.oms_id == "oms-north" and c.slot == 5 for c in fr.clashes)


def test_check_feasibility_typed_clash_names_shared_oms():
    g = _grid()
    n = _model(lit_slot=7)
    fr = check_spectrum_feasibility(n, ("oms-north",), 7, grid=g)
    assert isinstance(fr, FeasibilityResult)
    assert fr.feasible is False
    assert fr.clashes[0].oms_id == "oms-north"
    # A free slot is feasible with no clashes.
    ok = check_spectrum_feasibility(n, ("oms-north",), 8, grid=g)
    assert ok.feasible is True and ok.clashes == ()
