from __future__ import annotations
from dataclasses import replace
from typing import TYPE_CHECKING, Dict, Optional, Self, Tuple
from .ip_assets import Router, IPLink, Service
from .modes import ModeRegistry
from .optical_network import FrozenModelError, OpticalNetworkModel

if TYPE_CHECKING:
    from .spectrum import SpectrumGrid

# Re-exported for backwards compatibility: FrozenModelError is defined next to
# the freeze/clone machinery in optical_network, but callers have always
# imported it from here.
__all__ = ["FrozenModelError", "NetworkModel"]


class NetworkModel(OpticalNetworkModel):
    """The full multi-layer model: the optical layer (inherited from
    ``OpticalNetworkModel``) plus routers, IP links, and services, and the
    cross-layer couplings between them (CLAUDE.md's three couplings)."""

    def __init__(
        self, modes: ModeRegistry, grid: Optional["SpectrumGrid"] = None,
    ) -> None:
        super().__init__(modes, grid)
        self._routers: Dict[str, Router] = {}
        self._ip_links: Dict[str, IPLink] = {}
        self._services: Dict[str, Service] = {}

    # ------------------------------------------------------------------ clone

    def _copy_state_into(self, c: Self) -> None:
        """Extend the optical copy with the IP registries. Without this override
        every clone (hence every snapshot, since SnapshotStore routes through
        clone()) would silently lose routers, IP links, and services."""
        super()._copy_state_into(c)
        c._routers = dict(self._routers)
        c._ip_links = dict(self._ip_links)
        c._services = dict(self._services)

    # ---------------------------------------------------------------- IP layer

    def add_router(self, r: Router) -> None:
        self._check_mutable()
        if r.id in self._routers:
            raise ValueError(f"router {r.id!r} already exists")
        self._routers[r.id] = r

    def get_router(self, rid: str) -> Router:
        return self._routers[rid]

    def list_routers(self) -> Tuple[Router, ...]:
        return tuple(self._routers.values())

    def add_ip_link(self, link: IPLink) -> None:
        self._check_mutable()
        if link.lightpath_id not in self._lightpaths:
            raise ValueError(f"unknown lightpath {link.lightpath_id!r}")
        if link.a_router not in self._routers:
            raise ValueError(f"IP link {link.id!r}: unknown a_router {link.a_router!r}")
        if link.z_router not in self._routers:
            raise ValueError(f"IP link {link.id!r}: unknown z_router {link.z_router!r}")
        if link.id in self._ip_links:
            raise ValueError(f"IP link {link.id!r} already exists")
        self._ip_links[link.id] = link

    def get_ip_link(self, lid: str) -> IPLink:
        return self._ip_links[lid]

    def get_ip_link_lightpath_id(self, link_id: str) -> Optional[str]:
        """Resolve an IP link id to its bound lightpath id, or None if the IP
        link no longer exists (e.g. after remove_lightpath/remove_ip_link left
        a service's working_path/protection_path pointing at a removed link --
        a documented, valid state per remove_ip_link's own docstring). Callers
        walking a service's path must use this instead of a bare
        get_ip_link(...).lightpath_id, which raises on the dangling case."""
        link = self._ip_links.get(link_id)
        return link.lightpath_id if link is not None else None

    def list_ip_links(self) -> Tuple[IPLink, ...]:
        return tuple(self._ip_links.values())

    def ip_links_for_lightpath(self, lp_id: str) -> Tuple[str, ...]:
        if lp_id not in self._lightpaths:
            raise KeyError(lp_id)
        return tuple(
            link.id for link in self._ip_links.values()
            if link.lightpath_id == lp_id
        )

    def remove_ip_link(self, link_id: str) -> None:
        """Remove an IP link. A service still referencing it keeps the now-dangling
        id in its working/protection path; simulate_ip_routing reports that service
        dropped (reason 'link_removed') rather than raising. Idempotent."""
        self._check_mutable()
        self._ip_links.pop(link_id, None)

    def remove_lightpath(self, lp_id: str) -> None:
        """Tear a lightpath down: unbind every IP link riding it (teardown flips
        the bound IP link down — CLAUDE.md coupling 1), then delegate the optical
        half (drop the lightpath, its recorded QoT, and invalidate OMS-sharing
        survivors) to ``OpticalNetworkModel.remove_lightpath``.

        Idempotent, matching remove_ip_link: a second call on an already-gone id
        is a no-op, not a KeyError. The unknown-id guard must come BEFORE
        ip_links_for_lightpath, which raises KeyError on an unknown lightpath."""
        self._check_mutable()
        if lp_id not in self._lightpaths:
            return
        for link_id in self.ip_links_for_lightpath(lp_id):
            self._ip_links.pop(link_id, None)
        super().remove_lightpath(lp_id)

    # ---------------------------------------------------------------- services

    def add_service(self, s: Service) -> None:
        from .ip_routing import is_contiguous_path
        self._check_mutable()
        if s.id in self._services:
            raise ValueError(f"service {s.id!r} already exists")
        for ip in s.working_path:
            if ip not in self._ip_links:
                raise ValueError(f"Service {s.id!r}: unknown IP link {ip!r} in working_path")
        for ip in s.protection_path:
            if ip not in self._ip_links:
                raise ValueError(f"Service {s.id!r}: unknown IP link {ip!r} in protection_path")
        # S1-8 (creation-time counterpart): the setters already forbid a
        # non-contiguous path on every later mutation; without this check a
        # service could be *created* with one and never be re-validated.
        # Empty paths are legal (a demand not yet routed) and skip the check,
        # matching set_service_working_path/protection_path's own contract.
        if s.working_path and not is_contiguous_path(self, s.src_router, s.dst_router, s.working_path):
            raise ValueError(
                f"Service {s.id!r}: working_path does not connect "
                f"{s.src_router!r}->{s.dst_router!r}"
            )
        if s.protection_path and not is_contiguous_path(self, s.src_router, s.dst_router, s.protection_path):
            raise ValueError(
                f"Service {s.id!r}: protection_path does not connect "
                f"{s.src_router!r}->{s.dst_router!r}"
            )
        self._services[s.id] = s

    def get_service(self, sid: str) -> Service:
        return self._services[sid]

    def set_service_working_path(
        self, service_id: str, ip_path: Tuple[str, ...],
    ) -> None:
        """Set the service's intended working path (validated for connectivity only).

        S1-8: this is a statement of INTENT, not a guarantee the path is up. It
        deliberately does not check link-up status (margin >= 0) — restoration
        and make-before-break planning must be able to pin a path before its
        lightpaths are provisioned, or while a link is degraded mid-migration.
        `simulate_ip_routing` is the place that reads actual capacity/margin and
        reports real drops; this method never does.
        """
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

    def set_service_protection_path(
        self, service_id: str, ip_path: Tuple[str, ...],
    ) -> None:
        """Set the service's intended protection path (validated for connectivity
        only). Mirrors set_service_working_path's contract exactly: statement of
        INTENT, not a liveness guarantee -- margin/capacity viability is
        validate_plan's job (_protection_viability_findings), never this method's.
        """
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
        self._services[service_id] = replace(svc, protection_path=tuple(ip_path))

    def list_services(self) -> Tuple[Service, ...]:
        return tuple(self._services.values())

    # ---------------------------------------------------------------- derived capacity

    def ip_link_capacity_gbps(self, link_id: str) -> float:
        """Derived IP link capacity: the bound lightpath's mode bitrate, gated to
        0 when margin < 0. Raises LookupError when no QoT has been recorded yet
        (caller must recompute first — see ip_routing.simulate_ip_routing's guard).

        S5-6: QoT is stored as a single QoTState per lightpath (the worse of
        forward/backward, per gated_qot's min), so a per-direction asymmetric
        degradation — CLAUDE.md's storm-damages-one-fiber-direction scenario —
        cannot manifest as a directional IP capacity change; the IP layer is
        undirected by construction. Documented as a known modeling boundary, not
        a bug: directional IP capacity would require a QoTState keyed by
        (lightpath, direction), which no current caller needs.
        """
        link = self._ip_links[link_id]
        lp = self._lightpaths[link.lightpath_id]
        state = self._qot_state.get(lp.id)
        if state is None:
            raise LookupError(f"no QoT state recorded for lightpath {lp.id!r}")
        if state.margin_db < 0:
            return 0.0
        return self.modes.get(lp.mode_id).bitrate_gbps
