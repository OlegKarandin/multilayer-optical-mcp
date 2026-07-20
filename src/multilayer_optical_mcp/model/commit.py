"""Commit + reconcile: the gated, control-plane-agnostic write path.

dry_run simulates on a clone and returns the diff. A live commit validates,
requires explicit confirm, then actuates op-by-op through an injectable Actuator
(default: apply-all-succeed). The intended end-state is recorded as a snapshot;
reconcile diffs actual-vs-intended into typed Drift so a partial control-plane
failure surfaces as structured data, never prose (CLAUDE.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .network import NetworkModel
from .plan import Plan, apply_op
from .qot_results import QoTResultStore
from .snapshots import SnapshotStore, diff_models
from .validate import ValidationReport, recompute_if_possible, validate_plan

# An actuator applies one op to the live model and reports success. Raising or
# returning False both count as a failed op (the op is not applied).
Actuator = Callable[[NetworkModel, object], bool]


def full_actuator(model: NetworkModel, op) -> bool:
    apply_op(model, op)
    return True


def actuate(model: NetworkModel, plan: Plan, actuator: Actuator) -> Tuple[int, int]:
    """Run each op through the actuator on the live model. Returns
    (applied_count, failed_count). A failed op leaves the model unchanged for
    that op (the actuator must not partially apply)."""
    applied = failed = 0
    for op in plan.ops:
        try:
            ok = actuator(model, op)
        except Exception:
            ok = False
        if ok:
            applied += 1
        else:
            failed += 1
    return applied, failed


@dataclass(frozen=True)
class CommitResult:
    status: str                          # see commit_plan docstring
    dry_run: bool
    applied_ops: int
    failed_ops: int
    intended_snapshot_id: Optional[str]
    validation: Optional[ValidationReport]
    diff: Optional[dict]                 # simulated delta (dry_run) else None


@dataclass(frozen=True)
class Drift:
    registry: str
    kind: str                            # "added" | "removed" | "modified"
    asset_id: str


@dataclass(frozen=True)
class DriftReport:
    in_sync: bool
    drift: Tuple[Drift, ...]


def drift_from_diff(diff: dict) -> Tuple[Drift, ...]:
    """Flatten a registry diff into typed Drift entries. Orientation is
    diff_models(actual, intended): 'added' = in intended but missing from reality
    (an un-actuated op); 'removed' = in reality but not intended; 'modified' =
    both have it but they differ."""
    out: List[Drift] = []
    for registry, delta in diff.items():
        for kind in ("added", "removed", "modified"):
            for asset_id in delta.get(kind, ()):
                out.append(Drift(registry=registry, kind=kind, asset_id=asset_id))
    return tuple(out)


def commit_plan(
    store: SnapshotStore,
    plan: Plan,
    *,
    store_results: QoTResultStore,
    dry_run: bool = True,
    confirm: bool = False,
    actuator: Actuator = full_actuator,
    recompute: Callable[[NetworkModel, QoTResultStore], None] = recompute_if_possible,
    basis: str = "physical",
    level: str = "link",
    dropped_tolerance_gbps: float = 0.0,
) -> CommitResult:
    """Simulate (dry_run) or actuate (live) a plan against store.current().

    Status values:
      - "dry_run"                : simulated; ground truth untouched; diff returned.
      - "rejected"               : live, but validation found violations (or a plan
                                   error) — nothing actuated.
      - "requires_approval"      : live and clean, but confirm was not set.
      - "committed"              : live, clean, confirmed, all ops actuated.
      - "committed_with_failures": live, confirmed, but the actuator failed some ops
                                   — reconcile() will surface the drift.
    """
    current = store.current()

    # Always validate on a clone first. A bad-reference/duplicate-id plan comes
    # back as an INVALID_PLAN violation (report.ok is False -> rejected below), not
    # as a raise; the try/except is a belt-and-suspenders for any unexpected error.
    try:
        report = validate_plan(current, plan, store=store_results, basis=basis,
                               level=level, dropped_tolerance_gbps=dropped_tolerance_gbps)
    except Exception as exc:  # unexpected -> typed rejection, never a thrown error
        return CommitResult(status="rejected", dry_run=dry_run, applied_ops=0,
                            failed_ops=0, intended_snapshot_id=None,
                            validation=None, diff={"error": str(exc)})

    if dry_run:
        work = current.clone()
        try:
            for op in plan.ops:
                apply_op(work, op)
        except Exception as exc:
            return CommitResult(status="rejected", dry_run=True, applied_ops=0,
                                failed_ops=0, intended_snapshot_id=None,
                                validation=report, diff={"error": str(exc)})
        return CommitResult(status="dry_run", dry_run=True, applied_ops=len(plan.ops),
                            failed_ops=0, intended_snapshot_id=None,
                            validation=report, diff=diff_models(current, work))

    # Live path.
    if not report.ok:
        return CommitResult(status="rejected", dry_run=False, applied_ops=0,
                            failed_ops=0, intended_snapshot_id=None,
                            validation=report, diff=None)
    if not confirm:
        return CommitResult(status="requires_approval", dry_run=False, applied_ops=0,
                            failed_ops=0, intended_snapshot_id=None,
                            validation=report, diff=None)

    # Record the intended end-state (all ops applied on a clone) before actuating,
    # so reconcile has a target even if the control plane partially fails.
    intended = current.clone()
    for op in plan.ops:
        apply_op(intended, op)
    intended_id = store.put(intended)

    applied, failed = actuate(current, plan, actuator)

    # Post-actuation recompute: seed QoT for freshly-lit lightpaths on the LIVE
    # model, so a just-committed lightpath reports derived capacity instead of
    # reading dark via ip_link_capacity_gbps's LookupError path. This is the live
    # twin of the recompute validate_plan runs on its discarded clone, and of
    # scenario.settle. Best-effort: a recompute failure must not un-report a
    # successful actuation (live state is already mutated and the intended
    # snapshot recorded), so swallow it and leave QoT unseeded — today's behavior
    # — rather than raise out of a committed plan (CLAUDE.md: typed, never raises).
    try:
        recompute(current, store_results)
    except Exception:
        pass

    status = "committed" if failed == 0 else "committed_with_failures"
    return CommitResult(status=status, dry_run=False, applied_ops=applied,
                        failed_ops=failed, intended_snapshot_id=intended_id,
                        validation=report, diff=None)


def reconcile(store: SnapshotStore, intended_snapshot_id: str) -> DriftReport:
    """Diff store.current() (reality, after a live commit) against the intended
    end-state recorded at commit time. Empty diff => in_sync."""
    intended = store.get(intended_snapshot_id)
    diff = diff_models(store.current(), intended)
    drift = drift_from_diff(diff)
    return DriftReport(in_sync=not drift, drift=drift)
