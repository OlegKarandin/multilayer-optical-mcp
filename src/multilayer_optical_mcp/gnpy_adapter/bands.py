"""Frequency-band value object and the three nested bands the synthesizer uses.

S3-10: the GNPy synthesis config carries three distinct frequency ranges that
must sit in a strict superset chain::

    amplifier NF-fit band  ⊇  SI (channel) band  ⊇  transceiver tuning band

Previously these were three pairs of hand-typed ``f_min``/``f_max`` literals
scattered across ``synthesize.py`` (and a fourth copy of the SI upper edge in
``adapter.py``). Nothing tied them together, so an edit to one could silently
break the ⊇ relationship the physics assumes. This module encodes the chain
explicitly: the SI band is the single canonical literal, and the amp/transceiver
bands are *derived* from it by named guard margins, with the ⊇ invariant checked
at import time.

The SI band is canonical (not derived) on purpose: its edges feed
``automatic_nch(f_min, f_max, spacing)``, so a sub-GHz float drift there could
flip an integer channel count and move GSNR. The amp and transceiver edges only
bound the NF fit range and the transceiver tuning range respectively, so deriving
them by float arithmetic is safe.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    """A closed frequency interval ``[f_min_hz, f_max_hz]`` in Hz."""

    f_min_hz: float
    f_max_hz: float

    def __post_init__(self) -> None:
        if self.f_min_hz >= self.f_max_hz:
            raise ValueError(
                f"Band requires f_min_hz < f_max_hz, got "
                f"[{self.f_min_hz}, {self.f_max_hz}]"
            )

    def contains(self, other: "Band") -> bool:
        """True when ``self`` ⊇ ``other`` (closed-interval superset)."""
        return self.f_min_hz <= other.f_min_hz and other.f_max_hz <= self.f_max_hz

    def narrowed(self, low_guard_hz: float, high_guard_hz: float) -> "Band":
        """A sub-band inset by the given guard margins (moves edges inward)."""
        return Band(self.f_min_hz + low_guard_hz, self.f_max_hz - high_guard_hz)

    def widened(self, low_guard_hz: float, high_guard_hz: float) -> "Band":
        """A super-band outset by the given guard margins (moves edges outward)."""
        return Band(self.f_min_hz - low_guard_hz, self.f_max_hz + high_guard_hz)


# --- guard margins (Hz), the explicit derivation the ⊇ chain is built from ---

# The amp NF-fit band sits one guard band outside the SI band on each edge, so
# the flat-NF advanced_model is fit across the full channel range plus headroom.
# 50 GHz = half this repo's default 100 GHz channel spacing (SpectrumGrid,
# model/spectrum.py): SI_BAND tiles exactly 48 channels with zero slack, so the
# top channel's center sits flush with SI_BAND.f_max and its occupied band
# (center +- spacing/2) needs the full 50 GHz to clear GNPy's INCLUSIVE
# is_in_band check (gnpy/core/info.py:396-399: >=/<=, not >/<). The previous
# 25 GHz value silently demuxed the top channel out of every Edfa's NF-fit
# range on every path, in both directions (docs/2026-07-19-open-todos.md §6).
# tests/gnpy_adapter/test_bands.py pins this relationship so a future grid- or
# guard-value change that breaks it fails loudly instead of reintroducing the
# drop silently.
_AMP_GUARD_HZ = 50e9
# The transceiver tuning band is inset from the SI band at the low edge (extra
# roll-off headroom); its top edge is flush with the SI band's top edge.
_TRX_LOW_GUARD_HZ = 50e9
_TRX_HIGH_GUARD_HZ = 0.0

# SI is the canonical channel band (exact literals — see the module docstring).
SI_BAND = Band(191.3e12, 196.1e12)
# Derived, guard-banded neighbours.
AMP_BAND = SI_BAND.widened(_AMP_GUARD_HZ, _AMP_GUARD_HZ)
TRANSCEIVER_BAND = SI_BAND.narrowed(_TRX_LOW_GUARD_HZ, _TRX_HIGH_GUARD_HZ)

# Import-time invariant: the three bands form a strict superset chain. If a future
# edit to the guard margins or the SI literal breaks amp ⊇ SI ⊇ transceiver, this
# fails loudly at import rather than producing subtly wrong synthesis config.
assert AMP_BAND.contains(SI_BAND), "amp band must contain the SI band"
assert SI_BAND.contains(TRANSCEIVER_BAND), "SI band must contain the transceiver band"
