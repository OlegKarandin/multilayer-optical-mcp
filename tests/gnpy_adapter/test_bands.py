import pytest

from multilayer_optical_mcp.gnpy_adapter.bands import (
    Band, AMP_BAND, SI_BAND, TRANSCEIVER_BAND,
)


def test_band_rejects_inverted_interval():
    with pytest.raises(ValueError):
        Band(196.1e12, 191.3e12)


def test_band_contains_is_the_superset_relation():
    outer = Band(191.0e12, 196.5e12)
    inner = Band(191.3e12, 196.1e12)
    assert outer.contains(inner)
    assert not inner.contains(outer)
    assert inner.contains(inner)  # a band contains itself (closed interval)


def test_narrowed_insets_both_edges_by_the_guard():
    b = Band(191.3e12, 196.1e12).narrowed(50e9, 0.0)
    assert b.f_min_hz == pytest.approx(191.35e12)
    assert b.f_max_hz == pytest.approx(196.1e12)


def test_widened_outsets_both_edges_by_the_guard():
    b = Band(191.3e12, 196.1e12).widened(25e9, 25e9)
    assert b.f_min_hz == pytest.approx(191.275e12)
    assert b.f_max_hz == pytest.approx(196.125e12)


def test_three_bands_encode_amp_superset_si_superset_transceiver():
    # S3-10: the ⊇ chain is an invariant of the module, not a coincidence of
    # three hand-typed literals that can drift apart.
    assert AMP_BAND.contains(SI_BAND)
    assert SI_BAND.contains(TRANSCEIVER_BAND)
    # ... and each containment is strict at the guarded edges (distinct bands).
    assert AMP_BAND.f_min_hz < SI_BAND.f_min_hz < TRANSCEIVER_BAND.f_min_hz
    assert TRANSCEIVER_BAND.f_max_hz <= SI_BAND.f_max_hz < AMP_BAND.f_max_hz


def test_si_band_edges_are_the_exact_channel_grid_literals():
    # The SI band drives automatic_nch; its edges must stay the exact literals
    # (191.3 / 196.1 THz) so the design reference channel count never drifts.
    assert SI_BAND.f_min_hz == 191.3e12
    assert SI_BAND.f_max_hz == 196.1e12


def test_amp_guard_covers_half_the_default_grid_spacing():
    """S6-add (2026-07-25): AMP_BAND's guard must be >= half the default
    SpectrumGrid's channel spacing, so the top grid slot's occupied band
    (center +- spacing/2) clears GNPy's inclusive is_in_band check
    (gnpy/core/info.py:396-399). A future change to either the grid spacing
    or this guard that breaks the relationship must fail here, not silently
    drop a channel again."""
    from multilayer_optical_mcp.model.spectrum import SpectrumGrid
    from multilayer_optical_mcp.gnpy_adapter.bands import _AMP_GUARD_HZ
    grid = SpectrumGrid.default()
    assert _AMP_GUARD_HZ >= grid.spacing_hz / 2


def test_amp_band_covers_the_top_grid_slot_exactly():
    """The topmost channel's center sits flush with SI_BAND.f_max (48
    channels at 100 GHz tile SI_BAND with zero slack) — its occupied band
    (+-50 GHz) must fit inside AMP_BAND under GNPy's INCLUSIVE is_in_band
    check (>=/<=), not just approximately."""
    from multilayer_optical_mcp.model.spectrum import SpectrumGrid
    grid = SpectrumGrid.default()
    top_center = grid.freq(grid.num_slots - 1)
    assert top_center == pytest.approx(SI_BAND.f_max_hz)
    occupied_max = top_center + grid.spacing_hz / 2
    assert occupied_max <= AMP_BAND.f_max_hz
