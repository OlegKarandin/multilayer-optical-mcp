import math

import pytest
from pydantic import ValidationError

from multilayer_optical_mcp.model.violations import (
    ModeInfeasibleViolation, SpectrumClashViolation, IpLinkOverloadViolation,
    DroppedTrafficViolation, DisjointnessCollapseViolation,
    ProtectionNotViableViolation, ProtectionOversubscribedViolation,
    InvalidPlanViolation, ValidationReportModel, CommitResultModel,
)


def test_mode_infeasible_violation_construction():
    v = ModeInfeasibleViolation(
        state_index=0, asset_id="lpAB", transient=False,
        margin_db=-2.5, gsnr_db=13.0, required_gsnr_db=15.5,
        deficit_db=2.5, feasible_downshift_modes=["50G-QPSK"],
    )
    assert v.type == "mode_infeasible"
    assert v.margin_db == -2.5


def test_mode_infeasible_violation_sanitizes_only_in_json_mode():
    v = ModeInfeasibleViolation(
        state_index=0, asset_id="lpAB", transient=False,
        margin_db=float("-inf"), gsnr_db=float("-inf"), required_gsnr_db=15.5,
        deficit_db=float("inf"), feasible_downshift_modes=[],
    )
    json_dump = v.model_dump(mode="json")
    assert json_dump["margin_db"] == "-Infinity"
    assert json_dump["gsnr_db"] == "-Infinity"
    assert json_dump["deficit_db"] == "Infinity"

    python_dump = v.model_dump()
    assert python_dump["margin_db"] == float("-inf")
    assert math.isinf(python_dump["margin_db"]) and python_dump["margin_db"] < 0


def test_spectrum_clash_violation_construction():
    v = SpectrumClashViolation(
        state_index=1, asset_id="omsAB", transient=True,
        slot=4, lightpaths=["lp1", "lp2"],
        retune_candidates={"lp1": [5, 6], "lp2": []},
    )
    assert v.type == "spectrum_clash"
    assert v.retune_candidates["lp1"] == [5, 6]


def test_ip_link_overload_violation_construction():
    v = IpLinkOverloadViolation(
        state_index=0, asset_id="ipAB", transient=False,
        utilization=1.2, offered_gbps=120.0, capacity_gbps=100.0,
        offload_gbps=20.0,
    )
    assert v.type == "ip_link_overload"
    assert v.offload_gbps == 20.0


def test_dropped_traffic_violation_construction():
    v = DroppedTrafficViolation(
        state_index=2, asset_id="svc1", transient=False,
        reason="link_down", on_link="ipAB", demand_gbps=50.0,
    )
    assert v.type == "dropped_traffic"
    assert v.reason == "link_down"


def test_disjointness_collapse_violation_construction():
    v = DisjointnessCollapseViolation(
        state_index=0, asset_id="svc1", transient=False,
        basis="risk_group", level="link", level_applied=False,
        shared_assets=["fAB"], shared_groups=["rg1"],
    )
    assert v.type == "disjointness_collapse"
    assert v.shared_groups == ["rg1"]


def test_protection_not_viable_violation_construction():
    v = ProtectionNotViableViolation(
        state_index=0, asset_id="svc1", transient=False,
        demand_gbps=50.0, protection_capacity_gbps=0.0,
        dead_links=["ipCD"], bottleneck_link=None,
    )
    assert v.type == "protection_not_viable"
    assert v.dead_links == ["ipCD"]


def test_protection_oversubscribed_violation_construction():
    v = ProtectionOversubscribedViolation(
        state_index=0, asset_id="ipAB", transient=False,
        working_gbps=60.0, reserved_gbps=50.0, capacity_gbps=100.0,
        overflow_gbps=10.0, reserving_services=["svc1", "svc2"],
    )
    assert v.type == "protection_oversubscribed"
    assert v.reserving_services == ["svc1", "svc2"]


def test_invalid_plan_violation_construction():
    v = InvalidPlanViolation(
        state_index=0, asset_id=None, transient=False,
        message="unknown op 'frobnicate'", op_index=2,
    )
    assert v.type == "invalid_plan"
    assert v.op_index == 2


def test_invalid_plan_violation_op_index_defaults_to_none():
    v = InvalidPlanViolation(
        state_index=0, asset_id=None, transient=False,
        message="malformed plan",
    )
    assert v.op_index is None


def test_validation_report_model_dispatches_discriminated_union():
    mode_infeasible_dict = {
        "type": "mode_infeasible", "state_index": 0, "asset_id": "lpAB",
        "transient": False, "margin_db": -1.0, "gsnr_db": 14.0,
        "required_gsnr_db": 15.0, "deficit_db": 1.0,
        "feasible_downshift_modes": [],
    }
    spectrum_clash_dict = {
        "type": "spectrum_clash", "state_index": 1, "asset_id": "omsAB",
        "transient": False, "slot": 3, "lightpaths": ["lp1"],
        "retune_candidates": {"lp1": [4, 5]},
    }
    report = ValidationReportModel.model_validate({
        "ok": False, "num_states": 2,
        "violations": [mode_infeasible_dict, spectrum_clash_dict],
    })
    assert type(report.violations[0]) is ModeInfeasibleViolation
    assert type(report.violations[1]) is SpectrumClashViolation
    assert report.violations[0].margin_db == -1.0
    assert report.violations[1].slot == 3


def test_validation_report_model_rejects_unknown_discriminator():
    with pytest.raises(ValidationError):
        ValidationReportModel.model_validate({
            "ok": False, "num_states": 1,
            "violations": [{"type": "not_a_real_type", "state_index": 0,
                             "asset_id": None, "transient": False}],
        })


def test_commit_result_model_construction_with_embedded_validation_report():
    report = ValidationReportModel(ok=True, num_states=1, violations=[])
    result = CommitResultModel(
        status="committed", dry_run=False, applied_ops=3, failed_ops=0,
        intended_snapshot_id="snap1", validation=report, diff=None, failures=[],
    )
    assert result.status == "committed"
    assert result.validation.ok is True
