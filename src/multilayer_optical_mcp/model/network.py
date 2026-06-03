from __future__ import annotations
from typing import Dict, Tuple
from .assets import (
    OpticalNode, FiberType, Fiber, Amplifier, ROADM, Transceiver, OMS,
    Lightpath, Router, IPLink, Service, SRLG, RiskGroup,
)
from .modes import ModeRegistry
from .qot import QoTState


class NetworkModel:
    def __init__(self, modes: ModeRegistry) -> None:
        self.modes = modes
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

    # ------------------------------------------------------------------ types

    def register_fiber_type(self, ft: FiberType) -> None:
        self._fiber_types[ft.type_variety] = ft

    def get_fiber_type(self, type_variety: str) -> FiberType:
        return self._fiber_types[type_variety]

    def list_fiber_types(self) -> Tuple[FiberType, ...]:
        return tuple(self._fiber_types.values())

    # ---------------------------------------------------------------- optical nodes

    def add_optical_node(self, n: OpticalNode) -> None:
        self._optical_nodes[n.id] = n

    # ---------------------------------------------------------------- fibers

    def add_fiber(self, f: Fiber) -> None:
        if f.type_variety not in self._fiber_types:
            raise ValueError(f"unknown fiber type {f.type_variety!r}")
        self._fibers[f.id] = f

    def get_fiber(self, fid: str) -> Fiber:
        return self._fibers[fid]

    # ---------------------------------------------------------------- amplifiers

    def add_amplifier(self, a: Amplifier) -> None:
        self._amplifiers[a.id] = a

    def get_amplifier(self, aid: str) -> Amplifier:
        return self._amplifiers[aid]

    # ---------------------------------------------------------------- ROADMs / transceivers

    def add_roadm(self, r: ROADM) -> None:
        self._roadms[r.id] = r

    def add_transceiver(self, t: Transceiver) -> None:
        self._transceivers[t.id] = t

    # ---------------------------------------------------------------- OMS

    def add_oms(self, oms: OMS) -> None:
        for el in oms.elements:
            if el not in self._fibers and el not in self._amplifiers:
                raise ValueError(
                    f"OMS {oms.id!r}: element {el!r} is neither fiber nor amplifier"
                )
        self._oms[oms.id] = oms

    def get_oms(self, oid: str) -> OMS:
        return self._oms[oid]

    def list_oms(self) -> Tuple[OMS, ...]:
        return tuple(self._oms.values())

    # ---------------------------------------------------------------- lightpaths

    def add_lightpath(self, lp: Lightpath) -> None:
        self.modes.get(lp.mode_id)  # raises KeyError if unknown mode
        for oms_id in lp.oms_sequence:
            if oms_id not in self._oms:
                raise ValueError(f"unknown OMS {oms_id!r}")
        self._lightpaths[lp.id] = lp

    def get_lightpath(self, lpid: str) -> Lightpath:
        return self._lightpaths[lpid]

    def list_lightpaths(self) -> Tuple[Lightpath, ...]:
        return tuple(self._lightpaths.values())

    # ---------------------------------------------------------------- IP layer

    def add_router(self, r: Router) -> None:
        self._routers[r.id] = r

    def get_router(self, rid: str) -> Router:
        return self._routers[rid]

    def add_ip_link(self, link: IPLink) -> None:
        if link.lightpath_id not in self._lightpaths:
            raise ValueError(f"unknown lightpath {link.lightpath_id!r}")
        self._ip_links[link.id] = link

    def get_ip_link(self, lid: str) -> IPLink:
        return self._ip_links[lid]

    def list_ip_links(self) -> Tuple[IPLink, ...]:
        return tuple(self._ip_links.values())

    # ---------------------------------------------------------------- services / risk

    def add_service(self, s: Service) -> None:
        self._services[s.id] = s

    def add_srlg(self, g: SRLG) -> None:
        self._srlgs[g.id] = g

    def add_risk_group(self, g: RiskGroup) -> None:
        self._risk_groups[g.id] = g

    def list_services(self) -> Tuple[Service, ...]:
        return tuple(self._services.values())

    def list_srlgs(self) -> Tuple[SRLG, ...]:
        return tuple(self._srlgs.values())

    def list_risk_groups(self) -> Tuple[RiskGroup, ...]:
        return tuple(self._risk_groups.values())
