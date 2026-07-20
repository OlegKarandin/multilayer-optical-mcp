"""Spectrum state: fixed WDM grid + per-OMS slot bitmasks.

Spectrum occupancy is STORED, efficiently: one integer bit-vector per OMS, bit
`i` set iff slot `i` is lit on that OMS. Feasibility along a path is a bitwise
OR of the path's OMS masks; first-fit is the lowest zero bit; reserve sets the
bit on each OMS in the path. This is the fast-RSA representation — not a
violation of the IP-capacity 'derived, never stored' rule (which is about
capacity = f(mode), a different quantity).

The occupancy index is built from the model's lightpaths (each lightpath's
`center_freq_hz` maps to a slot via the grid). Solvers seed a run-local working
copy so in-progress reservations don't clash with each other.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

from .network import NetworkModel


class FillPolicy(Enum):
    """Acceptance-probe reference-loading policy (acceptance time only).

    ACTUAL — probe against the channels lit at probe time (a subset of the
      eventual load). Today's behavior; order-dependent, optimistic.
    FULL — probe against a fully-loaded comb (every non-probe slot lit). The
      delivered mode is chosen to stay feasible as the network fills:
      order-independent and margin-stable. By GSNR monotonicity in interferer
      count, a FULL-accepted mode remains feasible under any lighter real load,
      so the operating recompute stays ACTUAL and is not gated by this policy.
    """
    ACTUAL = "actual"
    FULL = "full"

# Default C-band grid: 48 channels @ 100 GHz (modulation_formats.yaml). Anchored
# at 191.4 THz so every slot (191.4–196.1 THz) lies inside gnpy's C-band and the
# toy carrier 193.4 THz stays on-grid (slot 20).
_DEFAULT_SPACING_HZ = 100e9
_DEFAULT_NUM_SLOTS = 48
_DEFAULT_ANCHOR_HZ = 191.4e12


@dataclass(frozen=True)
class SpectrumGrid:
    anchor_hz: float
    spacing_hz: float
    num_slots: int

    @classmethod
    def default(cls) -> "SpectrumGrid":
        return cls(anchor_hz=_DEFAULT_ANCHOR_HZ, spacing_hz=_DEFAULT_SPACING_HZ,
                   num_slots=_DEFAULT_NUM_SLOTS)

    def freq(self, slot: int) -> float:
        return self.anchor_hz + slot * self.spacing_hz

    def slot_of(self, freq_hz: float) -> int:
        slot = round((freq_hz - self.anchor_hz) / self.spacing_hz)
        if not 0 <= slot < self.num_slots:
            raise ValueError(f"frequency {freq_hz:.6e} Hz maps to out-of-grid slot {slot}")
        return slot

    @property
    def all_slots_mask(self) -> int:
        return (1 << self.num_slots) - 1


@dataclass(frozen=True)
class Clash:
    oms_id: str
    slot: int


@dataclass(frozen=True)
class FeasibilityResult:
    feasible: bool
    clashes: Tuple[Clash, ...] = ()


def build_spectrum_state(model: NetworkModel, grid: SpectrumGrid) -> Dict[str, int]:
    """Per-OMS slot bitmask built from every lightpath's center frequency."""
    state: Dict[str, int] = {}
    for lp in model.list_lightpaths():
        slot = grid.slot_of(lp.center_freq_hz)
        for oms_id in lp.oms_sequence:
            state[oms_id] = state.get(oms_id, 0) | (1 << slot)
    return state


def occupied_along(state: Dict[str, int], oms_sequence: Tuple[str, ...]) -> int:
    """Bitmask of slots occupied on *any* OMS along the path (bitwise OR)."""
    mask = 0
    for oms_id in oms_sequence:
        mask |= state.get(oms_id, 0)
    return mask


def free_slots_along(
    state: Dict[str, int], oms_sequence: Tuple[str, ...], grid: SpectrumGrid,
) -> int:
    """Bitmask of slots free on *every* OMS along the path."""
    return (~occupied_along(state, oms_sequence)) & grid.all_slots_mask


def first_fit_slot(
    state: Dict[str, int], oms_sequence: Tuple[str, ...], grid: SpectrumGrid,
    extra_state: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    """Lowest slot index free on every OMS along the path, or None."""
    occ = occupied_along(state, oms_sequence)
    if extra_state is not None:
        occ |= occupied_along(extra_state, oms_sequence)
    free = (~occ) & grid.all_slots_mask
    if free == 0:
        return None
    return (free & -free).bit_length() - 1  # index of lowest set bit


def reserve(state: Dict[str, int], oms_sequence: Tuple[str, ...], slot: int) -> None:
    """Set *slot* occupied on each OMS along the path (mutates *state*)."""
    bit = 1 << slot
    for oms_id in oms_sequence:
        state[oms_id] = state.get(oms_id, 0) | bit


def check_spectrum_feasibility(
    model: NetworkModel,
    oms_sequence: Tuple[str, ...],
    slot: int,
    *,
    grid: Optional[SpectrumGrid] = None,
    extra_state: Optional[Dict[str, int]] = None,
) -> FeasibilityResult:
    """Is *slot* free on every OMS along the path? Reports a typed clash naming
    each OMS that already occupies the slot. `extra_state` folds in reservations
    made earlier in an in-progress solver run."""
    grid = grid or SpectrumGrid.default()
    state = build_spectrum_state(model, grid)
    bit = 1 << slot
    clashes: list[Clash] = []
    for oms_id in oms_sequence:
        occ = state.get(oms_id, 0)
        if extra_state is not None:
            occ |= extra_state.get(oms_id, 0)
        if occ & bit:
            clashes.append(Clash(oms_id=oms_id, slot=slot))
    return FeasibilityResult(feasible=not clashes, clashes=tuple(clashes))
