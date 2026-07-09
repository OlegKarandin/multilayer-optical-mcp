from __future__ import annotations
from dataclasses import replace
from typing import TYPE_CHECKING, Dict, Optional, Tuple
from .assets import (
    OpticalNode, FiberType, Fiber, Amplifier, ROADM, Transceiver, OMS,
    Lightpath, Router, IPLink, Service, SRLG, RiskGroup,
)
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


class NetworkModel:
    def __init__(
        self, modes: ModeRegistry, grid: Optional["SpectrumGrid"] = None,
    ) -> None:
        self.modes = modes
        self._grid = grid
        self._frozen = False
        self._fiber_types: Dict[str, FiberType] = {}
        self._optical_nodes: Dict[str, OpticalNode] = {}
        self._fibers: Dict[str, Fiber] = {}
        self._amplifiers: Dict[str, Amplifier] = {}
        self._roadms: Dict[str, ROADM] = {}
        self._transceivers: Dict[str, Transceiver] = {}
        self._oms: Dict[str, OMS] = {}
        self._lightpaths: Dict[str, Lightpath] = {}
        self._routers: Dict[str, Router] = {}
        self._ip_links: Dict[str, IPLink] = {}
        self._services: Dict[str, Service] = {}
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

    def freeze(self) -> "NetworkModel":
        """Mark this model immutable and return it (for chaining). Every mutator
        then raises FrozenModelError."""
        self._frozen = True
        return self

    def clone(self) -> "NetworkModel":
        """Deep-ish copy of the model with independent collections. The single
        home for model duplication (SnapshotStore and Phase 7 both use it). The
        clone is always UNFROZEN regardless of this model's frozen state, so a
        frozen snapshot can be thawed into a working copy by cloning it."""
        c = NetworkModel(modes=self.modes, grid=self._grid)
        c._fiber_types = dict(self._fiber_types)
        c._optical_nodes = dict(self._optical_nodes)
        c._fibers = dict(self._fibers)
        c._amplifiers = dict(self._amplifiers)
        c._roadms = dict(self._roadms)
        c._transceivers = dict(self._transceivers)
        c._oms = dict(self._oms)
        c._lightpaths = dict(self._lightpaths)
        c._routers = dict(self._routers)
        c._ip_links = dict(self._ip_links)
        c._services = dict(self._services)
        c._srlgs = dict(self._srlgs)
        c._risk_groups = dict(self._risk_groups)
        c._qot_state = dict(self._qot_state)
        c._failed_assets = set(self._failed_assets)
        c._frozen = False
        return c

    # ------------------------------------------------------------------ types

    def register_fiber_type(self, ft: FiberType) -> None:
        self._check_mutable()
        self._fiber_types[ft.type_variety] = ft

    def get_fiber_type(self, type_variety: str) -> FiberType:
        return self._fiber_types[type_variety]

    def list_fiber_types(self) -> Tuple[FiberType, ...]:
        return tuple(self._fiber_types.values())

    # ---------------------------------------------------------------- optical nodes

    def add_optical_node(self, n: OpticalNode) -> None:
        self._check_mutable()
        self._optical_nodes[n.id] = n

    # ---------------------------------------------------------------- fibers

    def add_fiber(self, f: Fiber) -> None:
        self._check_mutable()
        if f.type_variety not in self._fiber_types:
            raise ValueError(f"unknown fiber type {f.type_variety!r}")
        self._fibers[f.id] = f

    def get_fiber(self, fid: str) -> Fiber:
        return self._fibers[fid]

    # ---------------------------------------------------------------- amplifiers

    def add_amplifier(self, a: Amplifier) -> None:
        self._check_mutable()
        self._amplifiers[a.id] = a

    def get_amplifier(self, aid: str) -> Amplifier:
        return self._amplifiers[aid]

    # ---------------------------------------------------------------- ROADMs / transceivers

    def add_roadm(self, r: ROADM) -> None:
        self._check_mutable()
        self._roadms[r.id] = r

    def add_transceiver(self, t: Transceiver) -> None:
        self._check_mutable()
        self._transceivers[t.id] = t

    def has_roadm(self, rid: str) -> bool:
        return rid in self._roadms

    # ---------------------------------------------------------------- OMS

    def add_oms(self, oms: OMS) -> None:
        self._check_mutable()
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

    def get_lightpath(self, lpid: str) -> Lightpath:
        return self._lightpaths[lpid]

    def list_lightpaths(self) -> Tuple[Lightpath, ...]:
        return tuple(self._lightpaths.values())

    # ---------------------------------------------------------------- IP layer

    def add_router(self, r: Router) -> None:
        self._check_mutable()
        self._routers[r.id] = r

    def get_router(self, rid: str) -> Router:
        return self._routers[rid]

    def add_ip_link(self, link: IPLink) -> None:
        self._check_mutable()
        if link.lightpath_id not in self._lightpaths:
            raise ValueError(f"unknown lightpath {link.lightpath_id!r}")
        self._ip_links[link.id] = link

    def get_ip_link(self, lid: str) -> IPLink:
        return self._ip_links[lid]

    def list_ip_links(self) -> Tuple[IPLink, ...]:
        return tuple(self._ip_links.values())

    def ip_links_for_lightpath(self, lp_id: str) -> Tuple[str, ...]:
        if lp_id not in self._lightpaths:
            raise KeyError(lp_id)
        return tuple(
            link.id for link in self._ip_links.values()
            if link.lightpath_id == lp_id
        )

    # ---------------------------------------------------------------- services / risk

    def add_service(self, s: Service) -> None:
        self._check_mutable()
        for ip in s.working_path:
            if ip not in self._ip_links:
                raise ValueError(f"Service {s.id!r}: unknown IP link {ip!r} in working_path")
        for ip in s.protection_path:
            if ip not in self._ip_links:
                raise ValueError(f"Service {s.id!r}: unknown IP link {ip!r} in protection_path")
        self._services[s.id] = s

    def get_service(self, sid: str) -> Service:
        return self._services[sid]

    def set_service_working_path(
        self, service_id: str, ip_path: Tuple[str, ...],
    ) -> None:
        from .ip_routing import is_contiguous_path
        self._check_mutable()
        svc = self._services[service_id]
        for ip_id in ip_path:
            if ip_id not in self._ip_links:
                raise ValueError(f"unknown IP link {ip_id!r}")
        if not is_contiguous_path(self, svc.src_router, svc.dst_router, ip_path):
            raise ValueError(
                f"ip_path does not connect {svc.src_router!r}->{svc.dst_router!r}"
            )
        self._services[service_id] = replace(svc, working_path=tuple(ip_path))

    def add_srlg(self, g: SRLG) -> None:
        self._check_mutable()
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

    def list_services(self) -> Tuple[Service, ...]:
        return tuple(self._services.values())

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

    # ---------------------------------------------------------------- derived capacity

    def ip_link_capacity_gbps(self, link_id: str) -> float:
        link = self._ip_links[link_id]
        lp = self._lightpaths[link.lightpath_id]
        state = self._qot_state.get(lp.id)
        if state is None:
            raise LookupError(f"no QoT state recorded for lightpath {lp.id!r}")
        if state.margin_db < 0:
            return 0.0
        return self.modes.get(lp.mode_id).bitrate_gbps

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
        """Clear specific failed assets, or all when asset_ids is empty."""
        self._check_mutable()
        if asset_ids:
            self._failed_assets.difference_update(asset_ids)
        else:
            self._failed_assets.clear()

    def failed_assets(self) -> frozenset:
        return frozenset(self._failed_assets)

    def is_failed(self, asset_id: str) -> bool:
        return asset_id in self._failed_assets
