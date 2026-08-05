"""Optical-layer network state, with zero knowledge of the IP layer.

``OpticalNetworkModel`` owns everything the physical layer needs — fiber types,
fibers, amplifiers, ROADMs, transceivers, OMS, lightpaths, SRLGs, risk groups,
recorded QoT, and the failed-asset set — and nothing else. It imports no IP
module (``ip_assets``, ``network``, ``ip_routing``) at any point, at module
scope or lazily, so the optical half of this package can be lifted into another
project on its own. ``tests/model/test_optical_network_model.py`` enforces that
with a fresh-subprocess import-isolation check.

``NetworkModel`` (``model/network.py``) subclasses this and adds routers, IP
links, services, and the cross-layer couplings on top.
"""

from __future__ import annotations
import math
from dataclasses import replace
from typing import TYPE_CHECKING, Dict, FrozenSet, Optional, Self, Tuple
from .assets import FiberType, Fiber, Amplifier, ROADM, Transceiver, OMS, Lightpath, SRLG, RiskGroup
from .modes import ModeRegistry
from .qot import QoTState

if TYPE_CHECKING:
    from .spectrum import SpectrumGrid


class FrozenModelError(RuntimeError):
    """Raised when a mutating method is called on a frozen (snapshot) model.

    Snapshots handed out by ``SnapshotStore.get()`` are frozen clones: they can
    be read but never mutated, so a caller cannot silently corrupt a stored
    snapshot for every future ``branch()``/``restore()``. Mutate a working copy
    obtained via ``branch()`` (``SnapshotStore.current()``) or ``clone()``."""


class OpticalNetworkModel:
    """Optical state only. Zero knowledge of Router/IPLink/Service.

    ``NetworkModel`` inherits from this class, so every mutator defined here is
    available on a ``NetworkModel`` automatically -- including ones added after
    this split. If a *future* optical mutator needs IP-side cleanup (the way
    ``remove_lightpath`` needs to unbind IP links), it will be silently
    inherited without that cleanup unless ``NetworkModel`` explicitly overrides
    it, the same way ``NetworkModel.remove_lightpath`` overrides this class's
    optical-only version. Treat "does this new optical mutator need an
    IP-layer override in NetworkModel?" as a standing review question whenever
    a mutator is added here.
    """

    def __init__(
        self, modes: ModeRegistry, grid: Optional["SpectrumGrid"] = None,
    ) -> None:
        self.modes = modes
        self._grid = grid
        self._frozen = False
        self._fiber_types: Dict[str, FiberType] = {}
        self._fibers: Dict[str, Fiber] = {}
        self._amplifiers: Dict[str, Amplifier] = {}
        self._roadms: Dict[str, ROADM] = {}
        self._transceivers: Dict[str, Transceiver] = {}
        self._oms: Dict[str, OMS] = {}
        self._lightpaths: Dict[str, Lightpath] = {}
        self._srlgs: Dict[str, SRLG] = {}
        self._risk_groups: Dict[str, RiskGroup] = {}
        self._qot_state: Dict[str, QoTState] = {}
        self._failed_assets: set[str] = set()

    # ------------------------------------------------------------------ freeze / clone

    def _check_mutable(self) -> None:
        if self._frozen:
            raise FrozenModelError(
                "cannot mutate a frozen snapshot model; branch() or clone() first"
            )

    def freeze(self) -> Self:
        """Mark this model immutable and return it (for chaining). Every mutator
        then raises FrozenModelError."""
        self._frozen = True
        return self

    def clone(self) -> Self:
        """Deep-ish copy of the model with independent collections. The single
        home for model duplication (SnapshotStore and Phase 7 both use it). The
        clone is always UNFROZEN regardless of this model's frozen state, so a
        frozen snapshot can be thawed into a working copy by cloning it.

        Template method: the new object is built with ``type(self)`` and its
        state filled in by ``_copy_state_into``, so a subclass (``NetworkModel``)
        only has to override ``_copy_state_into`` to have its extra registries
        copied. Hardcoding ``OpticalNetworkModel(...)`` here would make
        ``NetworkModel.clone()`` silently return an optical-only object and drop
        every router, IP link, and service — and since SnapshotStore's
        create/branch/get/restore/put all route through ``clone()``, that would
        corrupt every snapshot in the system without an obvious symptom."""
        c = type(self)(modes=self.modes, grid=self._grid)
        self._copy_state_into(c)
        c._frozen = False
        return c

    def _copy_state_into(self, c: Self) -> None:
        """Copy this model's registries into a fresh instance *c*. Subclasses
        override to add their own registries after calling ``super()``."""
        c._fiber_types = dict(self._fiber_types)
        c._fibers = dict(self._fibers)
        c._amplifiers = dict(self._amplifiers)
        c._roadms = dict(self._roadms)
        c._transceivers = dict(self._transceivers)
        c._oms = dict(self._oms)
        c._lightpaths = dict(self._lightpaths)
        c._srlgs = dict(self._srlgs)
        c._risk_groups = dict(self._risk_groups)
        c._qot_state = dict(self._qot_state)
        c._failed_assets = set(self._failed_assets)

    # ------------------------------------------------------------------ types

    def register_fiber_type(self, ft: FiberType) -> None:
        self._check_mutable()
        self._fiber_types[ft.type_variety] = ft

    def get_fiber_type(self, type_variety: str) -> FiberType:
        return self._fiber_types[type_variety]

    def list_fiber_types(self) -> Tuple[FiberType, ...]:
        return tuple(self._fiber_types.values())

    # ---------------------------------------------------------------- fibers

    def add_fiber(self, f: Fiber) -> None:
        self._check_mutable()
        if f.type_variety not in self._fiber_types:
            raise ValueError(f"unknown fiber type {f.type_variety!r}")
        if f.id in self._fibers:
            raise ValueError(f"fiber {f.id!r} already exists")
        self._fibers[f.id] = f

    def get_fiber(self, fid: str) -> Fiber:
        return self._fibers[fid]

    # ---------------------------------------------------------------- amplifiers

    def add_amplifier(self, a: Amplifier) -> None:
        self._check_mutable()
        if a.id in self._amplifiers:
            raise ValueError(f"amplifier {a.id!r} already exists")
        self._amplifiers[a.id] = a

    def get_amplifier(self, aid: str) -> Amplifier:
        return self._amplifiers[aid]

    # ---------------------------------------------------------------- ROADMs / transceivers

    def add_roadm(self, r: ROADM) -> None:
        self._check_mutable()
        if r.id in self._roadms:
            raise ValueError(f"ROADM {r.id!r} already exists")
        self._roadms[r.id] = r

    def add_transceiver(self, t: Transceiver) -> None:
        self._check_mutable()
        if t.id in self._transceivers:
            raise ValueError(f"transceiver {t.id!r} already exists")
        self._transceivers[t.id] = t

    def has_roadm(self, rid: str) -> bool:
        return rid in self._roadms

    # ---------------------------------------------------------------- OMS

    def add_oms(self, oms: OMS) -> None:
        self._check_mutable()
        if oms.id in self._oms:
            raise ValueError(f"OMS {oms.id!r} already exists")
        for el in oms.elements:
            if (el not in self._fibers
                    and el not in self._amplifiers
                    and el not in self._roadms):
                raise ValueError(
                    f"OMS {oms.id!r}: element {el!r} is neither fiber, amplifier, nor roadm"
                )
        self._oms[oms.id] = oms

    def get_oms(self, oid: str) -> OMS:
        return self._oms[oid]

    def list_oms(self) -> Tuple[OMS, ...]:
        return tuple(self._oms.values())

    # ---------------------------------------------------------------- lightpaths

    def add_lightpath(self, lp: Lightpath) -> None:
        self._check_mutable()
        if lp.id in self._lightpaths:
            raise ValueError(f"lightpath {lp.id!r} already exists")
        self.modes.get(lp.mode_id)  # raises KeyError if unknown mode
        for oms_id in lp.oms_sequence:
            if oms_id not in self._oms:
                raise ValueError(f"unknown OMS {oms_id!r}")
        # S1-4: the OMS sequence must physically chain (each OMS's dst_node must
        # equal the next OMS's src_node). A gap or inversion otherwise passes
        # silently and mislocates endpoints in _lightpath_endpoints (S7-1).
        seq = lp.oms_sequence
        for a, b in zip(seq, seq[1:]):
            oa, ob = self._oms[a], self._oms[b]
            if oa.dst_node_id != ob.src_node_id:
                raise ValueError(
                    f"lightpath {lp.id!r}: OMS chain break {a!r} "
                    f"(->{oa.dst_node_id!r}) does not meet {b!r} "
                    f"({ob.src_node_id!r}->)"
                )
        # S1-5: if the model carries a spectrum grid, validate the carrier is
        # on-grid now (grid.slot_of raises ValueError) rather than deferring the
        # error to build_spectrum_state at routing time.
        if self._grid is not None:
            self._grid.slot_of(lp.center_freq_hz)
        self._lightpaths[lp.id] = lp
        # A new channel changes NLI for every other channel co-propagating on a
        # shared OMS (more interferers -> lower GSNR for everyone on that
        # fiber). Unlike apply_nf_delta/apply_loss_delta's S1-7 blunt
        # clear-all (which would need an elements-membership walk to resolve
        # crossing here), the affected set is cheap and exact: lp.oms_sequence
        # is already the crossing membership, so invalidate only lightpaths
        # that actually share an OMS with the new one -- a lightpath on a
        # physically disjoint span is untouched. A cleared entry reads as
        # "unknown" (LookupError), never a stale value.
        self._invalidate_qot_sharing_oms(lp.oms_sequence, exclude=lp.id)

    def get_lightpath(self, lpid: str) -> Lightpath:
        return self._lightpaths[lpid]

    def list_lightpaths(self) -> Tuple[Lightpath, ...]:
        return tuple(self._lightpaths.values())

    def remove_lightpath(self, lp_id: str) -> None:
        """Tear a lightpath down at the OPTICAL layer: drop it, drop its recorded
        QoT, and invalidate the QoT of every survivor sharing one of its OMS.
        Removing (not sentinelling) frees the lightpath's spectrum slot and stops
        it loading its fibers, which is what make-before-break requires.
        Idempotent: a second call on an already-gone id is a no-op, not a
        KeyError.

        ``NetworkModel`` overrides this to first unbind the IP links riding the
        lightpath (teardown flips the bound IP link down — CLAUDE.md coupling 1)
        before delegating here."""
        self._check_mutable()
        lp = self._lightpaths.get(lp_id)
        if lp is None:
            return
        oms_sequence = lp.oms_sequence
        self._lightpaths.pop(lp_id, None)
        self._qot_state.pop(lp_id, None)
        # Dropping a channel changes NLI for every surviving channel that was
        # co-propagating with it on a shared OMS -- mirrors add_lightpath's
        # invalidation (see its comment). A lightpath on a physically disjoint
        # span is untouched.
        self._invalidate_qot_sharing_oms(oms_sequence)

    def _invalidate_qot_sharing_oms(
        self, oms_sequence: Tuple[str, ...], exclude: Optional[str] = None,
    ) -> None:
        """Drop recorded QoT for every lightpath that shares at least one OMS
        with *oms_sequence* (a channel add/remove on those spans). *exclude*
        skips a given lightpath id (the one just added, which has no prior
        state to invalidate anyway)."""
        oms_set = set(oms_sequence)
        if not oms_set:
            return
        for other in self._lightpaths.values():
            if other.id == exclude:
                continue
            if oms_set.intersection(other.oms_sequence):
                self._qot_state.pop(other.id, None)

    # ---------------------------------------------------------------- risk

    def add_srlg(self, g: SRLG) -> None:
        self._check_mutable()
        if g.id in self._srlgs:
            raise ValueError(f"SRLG {g.id!r} already exists")
        self._srlgs[g.id] = g

    def get_srlg(self, gid: str) -> SRLG:
        return self._srlgs[gid]

    def get_srlg_members(self, gid: str) -> Tuple[str, ...]:
        return self._srlgs[gid].asset_ids

    def add_risk_group(self, g: RiskGroup) -> None:
        self._check_mutable()
        self._risk_groups[g.id] = g

    def get_risk_group(self, gid: str) -> RiskGroup:
        return self._risk_groups[gid]

    def define_risk_group(
        self,
        rg_id: str,
        asset_ids: Tuple[str, ...],
        metadata: Optional[dict] = None,
    ) -> RiskGroup:
        """Permissive runtime risk-group constructor. asset_ids are NOT
        validated against the model — risk groups are abstract partitions
        and a downstream app may reference assets this server does not own.
        Reject only on duplicate id."""
        self._check_mutable()
        if rg_id in self._risk_groups:
            raise ValueError(f"risk group {rg_id!r} already exists")
        rg = RiskGroup(id=rg_id, asset_ids=tuple(asset_ids),
                       metadata=dict(metadata or {}))
        self._risk_groups[rg_id] = rg
        return rg

    def list_srlgs(self) -> Tuple[SRLG, ...]:
        return tuple(self._srlgs.values())

    def list_risk_groups(self) -> Tuple[RiskGroup, ...]:
        return tuple(self._risk_groups.values())

    # ---------------------------------------------------------------- QoT state

    def set_qot_state(self, lp_id: str, state: QoTState) -> None:
        self._check_mutable()
        if lp_id not in self._lightpaths:
            raise KeyError(lp_id)
        self._qot_state[lp_id] = state

    def get_qot_state(self, lp_id: str) -> QoTState:
        if lp_id not in self._qot_state:
            raise LookupError(f"no QoT state recorded for lightpath {lp_id!r}")
        return self._qot_state[lp_id]

    # ---------------------------------------------------------------- mode mutation

    def set_lightpath_mode(self, lp_id: str, mode_id: str) -> None:
        self._check_mutable()
        self.modes.get(mode_id)  # raises KeyError if unknown mode
        lp = self._lightpaths[lp_id]
        self._lightpaths[lp_id] = replace(lp, mode_id=mode_id)
        # S1-7: the mode's required-GSNR threshold changed, so this lightpath's
        # margin (hence derived capacity) is stale until recompute. Clear it so
        # ip_link_capacity_gbps reports "unknown" rather than a stale value.
        self._qot_state.pop(lp_id, None)

    # ---------------------------------------------------------------- injection mutators

    def apply_nf_delta(self, amp_id: str, delta_db: float) -> None:
        """Add delta_db to an amplifier's NF (branch what-if). Raises KeyError if unknown."""
        self._check_mutable()
        amp = self._amplifiers[amp_id]
        self._amplifiers[amp_id] = replace(amp, nf_db=amp.nf_db + delta_db)
        # S1-7: any lightpath crossing this amp is now stale. Rather than resolve
        # crossing membership here, clear all QoT — recompute repopulates it.
        self._qot_state.clear()

    def apply_loss_delta(self, fiber_id: str, delta_db: float) -> None:
        """Add delta_db of lumped loss to a fiber (branch what-if). Raises KeyError if unknown."""
        self._check_mutable()
        f = self._fibers[fiber_id]
        self._fibers[fiber_id] = replace(f, extra_loss_db=f.extra_loss_db + delta_db)
        # S1-7: any lightpath crossing this fiber is now stale (see apply_nf_delta).
        self._qot_state.clear()

    def mark_failed(self, asset_ids: Tuple[str, ...]) -> None:
        """Mark assets failed on this (branch) model."""
        self._check_mutable()
        self._failed_assets.update(asset_ids)

    def clear_failed(self, asset_ids: Tuple[str, ...] = ()) -> None:
        """Clear specific failed assets, or all when asset_ids is empty.

        S8-6: also drop the -inf QoT sentinels that inject_failure wrote for any
        lightpath that no longer crosses a *remaining* failed asset, so
        _failed_assets and _qot_state can't disagree. The dropped entries read as
        "unknown" (LookupError) until the next recompute — the honest state, since
        a cleared asset's real QoT is not known without recomputing."""
        self._check_mutable()
        if asset_ids:
            self._failed_assets.difference_update(asset_ids)
        else:
            self._failed_assets.clear()
        remaining = frozenset(self._failed_assets)
        for lp in self._lightpaths.values():
            st = self._qot_state.get(lp.id)
            if st is None or not (math.isinf(st.margin_db) and st.margin_db < 0):
                continue  # not a failure sentinel — leave real QoT untouched
            if not (lightpath_footprint(self, lp.oms_sequence) & remaining):
                self._qot_state.pop(lp.id, None)

    def failed_assets(self) -> frozenset:
        return frozenset(self._failed_assets)

    def is_failed(self, asset_id: str) -> bool:
        return asset_id in self._failed_assets


# ---------------------------------------------------------------------------
# Footprint helpers.
#
# These live here, next to the optical model, rather than in ``exposure.py``:
# they read only ``get_oms``/``has_roadm`` (both optical), and ``clear_failed``
# above needs ``lightpath_footprint``. Keeping them in ``exposure.py`` would
# force a lazy ``from .exposure import ...`` inside ``clear_failed``, and since
# ``exposure`` imports ``NetworkModel`` at module scope, the first
# ``clear_failed()`` call on a "standalone" optical model would drag in the
# entire IP layer. ``exposure.py`` re-exports all three, so its consumers are
# unaffected.
# ---------------------------------------------------------------------------


def oms_seq_asset_set(
    model: OpticalNetworkModel, oms_sequence: Tuple[str, ...],
) -> FrozenSet[str]:
    """Expand an OMS-id sequence to {oms_ids} ∪ {fiber/amp/roadm uids}. Shared
    by IP-link expansion (in ``exposure.py``) and the routing/disjointness
    solvers, which work with OMS-sequences directly."""
    assets: set[str] = set()
    for oms_id in oms_sequence:
        assets.add(oms_id)
        oms = model.get_oms(oms_id)
        assets.update(oms.elements)
    return frozenset(assets)


def terminal_roadm_id(
    model: OpticalNetworkModel, oms_sequence: Tuple[str, ...],
) -> "str | None":
    """The drop ROADM at the end of *oms_sequence*, or None.

    Each importer OMS's ``elements`` start at ``roadm_<src>`` but omit the drop
    ROADM (``roadm_<dst>``), so ``oms_seq_asset_set`` never includes it. Callers
    that must reason about the whole physical footprint — e.g. failing the
    destination ROADM (S8-3) — add this. Returns None when the path drops into a
    bare transceiver (no ``roadm_<dst>`` registered), as in legacy toy models.
    """
    if not oms_sequence:
        return None
    last = model.get_oms(oms_sequence[-1])
    candidate = f"roadm_{last.dst_node_id}"
    return candidate if model.has_roadm(candidate) else None


def lightpath_footprint(
    model: OpticalNetworkModel, oms_sequence: Tuple[str, ...],
) -> FrozenSet[str]:
    """The full physical footprint of an OMS-sequence: ``oms_seq_asset_set`` plus
    the terminal drop ROADM the OMS elements omit (S8-3).

    This is the single crossing predicate shared by every failure-aware path —
    ``inject_failure`` (down a lightpath), ``recompute_qot_under_loading``
    (don't resurrect a downed lightpath, S8-1), and ``clear_failed`` (drop the
    sentinel only when nothing failed still crosses it, S8-6) — so the failed-set
    and the QoT sentinels can never disagree about what a lightpath crosses."""
    assets = set(oms_seq_asset_set(model, oms_sequence))
    term = terminal_roadm_id(model, oms_sequence)
    if term is not None:
        assets.add(term)
    return frozenset(assets)
