"""A plan is an ordered sequence of typed operations. Each op mutates a branch
model in place via apply_op; replay applies a whole plan to a fresh clone so the
input model is never touched. validate_plan/commit_plan drive these.

Decision 1 (plan doc): modelling a plan as a *sequence replayed op-by-op* — not a
before/after pair — is what makes "check every intermediate state" concrete. The
make-before-break overlap is not a special construction: it is simply the state
between a provision and its matching teardown, where both channels exist and
loading_from_model already returns both.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Tuple, Union

from .assets import IPLink, Lightpath
from .network import NetworkModel


@dataclass(frozen=True)
class ProvisionLightpath:
    """Light a new lightpath; optionally bind and bring up an IP link on it."""
    lightpath: Lightpath
    ip_link: Optional[IPLink] = None


@dataclass(frozen=True)
class TeardownLightpath:
    lightpath_id: str


@dataclass(frozen=True)
class RerouteService:
    service_id: str
    ip_path: Tuple[str, ...]
    which: str = "working"


@dataclass(frozen=True)
class SetModulationFormat:
    lightpath_id: str
    mode_id: str


PlanOp = Union[ProvisionLightpath, TeardownLightpath, RerouteService, SetModulationFormat]


@dataclass(frozen=True)
class Plan:
    ops: Tuple[PlanOp, ...]


class PlanError(ValueError):
    """A plan op references something the model does not contain, or is malformed.
    validate_plan / commit_plan catch this and surface it as a typed INVALID_PLAN
    result rather than propagating an exception (CLAUDE.md: results are structured,
    never prose)."""


def apply_op(model: NetworkModel, op: PlanOp) -> None:
    """Apply a single op to model in place. Raises PlanError on a bad reference or
    a duplicate provision. The duplicate-id guard lives here (not in the validator)
    so the live single-op mutation tools reject a duplicate identically."""
    if isinstance(op, ProvisionLightpath):
        if op.lightpath.id in model._lightpaths:
            raise PlanError(
                f"provision: lightpath {op.lightpath.id!r} already exists")
        if op.ip_link is not None and op.ip_link.id in model._ip_links:
            raise PlanError(
                f"provision: ip_link {op.ip_link.id!r} already exists")
        model.add_lightpath(op.lightpath)
        if op.ip_link is not None:
            # Bind the IP link to the just-provisioned lightpath, regardless of
            # whatever lightpath_id the caller put on it.
            model.add_ip_link(replace(op.ip_link, lightpath_id=op.lightpath.id))
    elif isinstance(op, TeardownLightpath):
        if op.lightpath_id not in model._lightpaths:
            raise PlanError(f"teardown: unknown lightpath {op.lightpath_id!r}")
        model.remove_lightpath(op.lightpath_id)
    elif isinstance(op, RerouteService):
        if op.service_id not in model._services:
            raise PlanError(f"reroute: unknown service {op.service_id!r}")
        if op.which == "working":
            setter = model.set_service_working_path
        elif op.which == "protection":
            setter = model.set_service_protection_path
        else:
            raise PlanError(f"reroute {op.service_id!r}: unrecognized which {op.which!r}")
        try:
            setter(op.service_id, tuple(op.ip_path))
        except ValueError as exc:
            raise PlanError(f"reroute {op.service_id!r}: {exc}") from exc
    elif isinstance(op, SetModulationFormat):
        if op.lightpath_id not in model._lightpaths:
            raise PlanError(f"set_modulation_format: unknown lightpath {op.lightpath_id!r}")
        try:
            model.set_lightpath_mode(op.lightpath_id, op.mode_id)
        except KeyError as exc:
            raise PlanError(f"set_modulation_format: unknown mode {op.mode_id!r}") from exc
    else:  # pragma: no cover - exhaustive
        raise PlanError(f"unknown op {op!r}")


def replay(model: NetworkModel, plan: Plan) -> NetworkModel:
    """Apply a whole plan to a fresh clone and return it. Input model untouched."""
    work = model.clone()
    for op in plan.ops:
        apply_op(work, op)
    return work


def service_oms_sequence(model: NetworkModel, ip_path: Tuple[str, ...]) -> Tuple[str, ...]:
    """Concatenate the OMS sequences of the lightpaths under an IP path. Used by
    the disjointness-collapse check to map IP working/protection paths to the
    OMS-sequence form check_disjointness audits."""
    seq: list[str] = []
    for ip_id in ip_path:
        lp = model.get_lightpath(model.get_ip_link(ip_id).lightpath_id)
        seq.extend(lp.oms_sequence)
    return tuple(seq)


def plan_from_dict(data: dict) -> Plan:
    """Parse the MCP-facing plan JSON into typed ops.

    {"ops": [
      {"op": "provision_lightpath",
       "lightpath": {id, oms_sequence, mode_id, center_freq_hz},
       "ip_link": {id, a_router, z_router} | null},
      {"op": "teardown_lightpath", "lightpath_id": ...},
      {"op": "reroute_service", "service_id": ..., "ip_path": [...],
       "which": "working"|"protection"  # optional, defaults "working"},
      {"op": "set_modulation_format", "lightpath_id": ..., "mode_id": ...}]}
    """
    ops: list[PlanOp] = []
    for raw in data.get("ops", []):
        kind = raw["op"]
        if kind == "provision_lightpath":
            lp = raw["lightpath"]
            lightpath = Lightpath(
                id=lp["id"], oms_sequence=tuple(lp["oms_sequence"]),
                mode_id=lp["mode_id"], center_freq_hz=lp["center_freq_hz"])
            ipl = raw.get("ip_link")
            ip_link = None if ipl is None else IPLink(
                id=ipl["id"], a_router=ipl["a_router"], z_router=ipl["z_router"],
                lightpath_id=lightpath.id)
            ops.append(ProvisionLightpath(lightpath=lightpath, ip_link=ip_link))
        elif kind == "teardown_lightpath":
            ops.append(TeardownLightpath(lightpath_id=raw["lightpath_id"]))
        elif kind == "reroute_service":
            ops.append(RerouteService(service_id=raw["service_id"],
                                      ip_path=tuple(raw["ip_path"]),
                                      which=raw.get("which", "working")))
        elif kind == "set_modulation_format":
            ops.append(SetModulationFormat(lightpath_id=raw["lightpath_id"],
                                           mode_id=raw["mode_id"]))
        else:
            raise PlanError(f"unknown op kind {kind!r}")
    return Plan(ops=tuple(ops))
