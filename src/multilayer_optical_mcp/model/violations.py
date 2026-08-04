"""Agent-visible JSON schema for validate_plan/commit_plan's violation output.

A discriminated union of flat Pydantic models -- one per ViolationType in
validate.py, with validate.py's `detail` dict keys promoted to top-level
fields matching each finding-builder's dict literal 1:1 (see validate.py's
_mode_infeasible_findings, _spectrum_clash_findings, _ip_findings,
_disjointness_findings, _protection_viability_findings,
_protection_oversubscription_findings, and the INVALID_PLAN construction site
in validate_plan itself).

This module ONLY changes what MCP tool functions in server.py are annotated
to RETURN (`-> ValidationReportModel` / `-> CommitResultModel` instead of
bare `-> dict`), so FastMCP publishes a real outputSchema. Every tool
function's body keeps building and returning a plain Python dict --
FastMCP's Tool.run()/convert_result() validates that dict against the
annotated model only on the real MCP-protocol-serving path. Every test in
this repo calls `.fn(**kwargs)` directly (bypassing that conversion step),
so views.py's hand-built dict shape is what tests actually see and must
keep matching these models field-for-field (see tests/model/test_views.py's
cross-validation regression test).
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, PlainSerializer

from .json_safety import safe_float

# A float that serializes to the RFC-8259-safe "-Infinity"/"Infinity"/"NaN"
# string sentinel ONLY when dumped in JSON mode (model_dump(mode="json") or
# model_dump_json()); model_dump() in python mode (and all internal access)
# keeps the real float, including a non-finite one -- mirrors json_safety's
# "only the outgoing dict is sanitized" contract.
SafeFloat = Annotated[
    float, PlainSerializer(safe_float, return_type=Union[float, str], when_used="json")
]


class _ViolationBase(BaseModel):
    state_index: int
    asset_id: Optional[str] = None
    transient: bool


class ModeInfeasibleViolation(_ViolationBase):
    """Fields match validate.py's _mode_infeasible_findings detail dict."""
    type: Literal["mode_infeasible"] = "mode_infeasible"
    margin_db: SafeFloat
    gsnr_db: SafeFloat
    required_gsnr_db: SafeFloat
    deficit_db: SafeFloat
    feasible_downshift_modes: list[str]


class SpectrumClashViolation(_ViolationBase):
    """Fields match validate.py's _spectrum_clash_findings detail dict."""
    type: Literal["spectrum_clash"] = "spectrum_clash"
    slot: int
    lightpaths: list[str]
    retune_candidates: dict[str, list[int]]


class IpLinkOverloadViolation(_ViolationBase):
    """Fields match validate.py's _ip_findings IP_LINK_OVERLOAD detail dict."""
    type: Literal["ip_link_overload"] = "ip_link_overload"
    utilization: SafeFloat
    offered_gbps: SafeFloat
    capacity_gbps: SafeFloat
    offload_gbps: SafeFloat


class DroppedTrafficViolation(_ViolationBase):
    """Fields match validate.py's _ip_findings DROPPED_TRAFFIC detail dict."""
    type: Literal["dropped_traffic"] = "dropped_traffic"
    reason: str
    on_link: str
    demand_gbps: SafeFloat


class DisjointnessCollapseViolation(_ViolationBase):
    """Fields match validate.py's _disjointness_findings detail dict."""
    type: Literal["disjointness_collapse"] = "disjointness_collapse"
    basis: str
    level: str
    shared_assets: list[str]
    shared_groups: list[str]


class ProtectionNotViableViolation(_ViolationBase):
    """Fields match validate.py's _protection_viability_findings detail dict."""
    type: Literal["protection_not_viable"] = "protection_not_viable"
    demand_gbps: SafeFloat
    protection_capacity_gbps: SafeFloat
    dead_links: list[str]
    bottleneck_link: Optional[str] = None


class ProtectionOversubscribedViolation(_ViolationBase):
    """Fields match validate.py's _protection_oversubscription_findings detail dict."""
    type: Literal["protection_oversubscribed"] = "protection_oversubscribed"
    working_gbps: SafeFloat
    reserved_gbps: SafeFloat
    capacity_gbps: SafeFloat
    overflow_gbps: SafeFloat
    reserving_services: list[str]


class InvalidPlanViolation(_ViolationBase):
    """Fields match validate.py's INVALID_PLAN construction in validate_plan
    (message/op_index) and server.py's own exception-path dict (message
    only, no op_index -- that path isn't tied to a specific op)."""
    type: Literal["invalid_plan"] = "invalid_plan"
    message: str
    op_index: Optional[int] = None


ViolationModel = Annotated[
    Union[
        ModeInfeasibleViolation, SpectrumClashViolation, IpLinkOverloadViolation,
        DroppedTrafficViolation, DisjointnessCollapseViolation,
        ProtectionNotViableViolation, ProtectionOversubscribedViolation,
        InvalidPlanViolation,
    ],
    Field(discriminator="type"),
]


class ValidationReportModel(BaseModel):
    ok: bool
    num_states: int
    violations: list[ViolationModel]


class OpFailureModel(BaseModel):
    op_index: int
    op: str
    error: str


class CommitResultModel(BaseModel):
    status: str
    dry_run: bool
    applied_ops: int
    failed_ops: int
    intended_snapshot_id: Optional[str] = None
    validation: Optional[ValidationReportModel] = None
    diff: Optional[dict] = None
    failures: list[OpFailureModel] = []
