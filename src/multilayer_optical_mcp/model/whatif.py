"""Branch-scoped what-if + injection.

Margin is an OUTPUT: the sweep screens recorded margin; inject_degradation
perturbs a physical parameter and lets margin move via recompute. None of these
mutate ground truth — callers pass a branch model.

Stage 8 assumptions (recorded explicitly, from the inspection roadmap):
- Injection mutates the model's physical parameters in place on a BRANCH;
  correctness depends on snapshots._clone copying the mutated
  _amplifiers/_fibers AND _failed_assets, and diff() reporting failed_assets
  — verified present.
- `inject_failure`'s -inf QoT sentinel — not `_failed_assets` alone — is the
  mechanism that reaches `simulate_ip_routing` (see
  ip_routing.simulate_ip_routing's S5-7 docstring for how a bare
  model.mark_failed() without a subsequent recompute would be missed there).
- `margin_threshold_sweep` is a pure read; lightpaths with no recorded QoT are
  omitted, never defaulted. Margin is never an input anywhere in this module.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple

from ..gnpy_adapter.loading import Channel, LoadingState
from ..gnpy_adapter.adapter import (
    compute_qot, recompute_qot_under_loading, isolate_element_contribution,
)
from .network import NetworkModel
from .qot import QoTState
from .qot_results import QoTResultStore
from .exposure import lightpath_footprint


@dataclass(frozen=True)
class MarginSweepRow:
    lightpath_id: str
    margin_db: float
    gsnr_db: float
    mode_feasible: bool


def loading_from_model(model: NetworkModel) -> LoadingState:
    """One channel per committed lightpath at its center frequency.

    Deliberately NOT built via LoadingState.union(): this is a network-wide
    comb spanning every lightpath regardless of which OMS it rides, so two
    lightpaths legitimately sharing a center frequency on physically disjoint
    fibers (ordinary wavelength reuse) is expected and must NOT raise here.
    recompute_qot_under_loading (the sole consumer of this function's output)
    already documents this: it reduces `loading.channels` to a per-grid-slot
    occupancy bitmask and intersects it with each lightpath's OWN per-OMS
    occupancy (S4-6/S8-5) rather than trusting frequency uniqueness across the
    whole comb, precisely so cross-fiber reuse doesn't look like a clash. A
    genuine same-fiber, same-slot clash is a different, already-covered check:
    validate.py's _spectrum_clash_findings (per-OMS occupancy, not this
    function) and add_lightpath's on-grid check -- see network.py."""
    channels = []
    for lp in model.list_lightpaths():
        mode = model.modes.get(lp.mode_id)
        channels.append(Channel(
            center_freq_hz=lp.center_freq_hz,
            slot_width_hz=mode.channel_spacing_hz,
            power_dbm=None,  # S2-2: use the adapter tx_power default
            mode_id=lp.mode_id,
            baud_rate_hz=mode.symbol_rate_baud,  # S2-4: per-carrier spectral shape
        ))
    return LoadingState(channels=tuple(channels))


def margin_threshold_sweep(model: NetworkModel, threshold_db: float) -> List[MarginSweepRow]:
    """Pure read: lightpaths whose recorded margin_db <= threshold_db, ascending.

    Physics-free screening (CLAUDE.md). Lightpaths with no recorded QoT are omitted.
    """
    rows: List[MarginSweepRow] = []
    for lp in model.list_lightpaths():
        try:
            st = model.get_qot_state(lp.id)
        except LookupError:
            continue
        if st.margin_db <= threshold_db:
            rows.append(MarginSweepRow(
                lightpath_id=lp.id,
                margin_db=st.margin_db,
                gsnr_db=st.gsnr_db,
                mode_feasible=st.mode_feasible,
            ))
    rows.sort(key=lambda r: r.margin_db)
    return rows


@dataclass(frozen=True)
class MaxFeasibleModeRow:
    lightpath_id: str
    current_mode: str
    max_feasible_mode: str | None      # None => GSNR below every mode's threshold
    direction: str                     # "headroom" | "downshift" | "match" | "infeasible"


def max_feasible_mode_view(model: NetworkModel) -> List[MaxFeasibleModeRow]:
    """Advisory read: per-lightpath current mode vs. highest GSNR-feasible mode.

    Reads the lightpath's *recorded* GSNR (no GNPy call) and maps it to the
    highest-bitrate mode whose required GSNR it meets. `direction` classifies that
    ceiling against the frozen current mode: `headroom` (could upshift), `downshift`
    (current mode no longer feasible; a lower mode is), `match` (already at the
    ceiling), or `infeasible` (GSNR below every mode). Lightpaths with no recorded
    QoT are omitted, never defaulted (same discipline as `margin_threshold_sweep`).

    This never mutates and never gates — mode stays a configured input. The
    `downshift`/`infeasible` rows overlap `validate_plan`'s MODE_INFEASIBLE finding
    by design; this view informs, validation gates."""
    rows: List[MaxFeasibleModeRow] = []
    for lp in model.list_lightpaths():
        try:
            st = model.get_qot_state(lp.id)
        except LookupError:
            continue
        feasible = [m for m in model.modes.list()
                    if m.required_gsnr_db <= st.gsnr_db]
        if not feasible:
            rows.append(MaxFeasibleModeRow(lp.id, lp.mode_id, None, "infeasible"))
            continue
        best = max(feasible, key=lambda m: m.bitrate_gbps)
        current_bitrate = model.modes.get(lp.mode_id).bitrate_gbps
        if best.bitrate_gbps > current_bitrate:
            direction = "headroom"
        elif best.bitrate_gbps < current_bitrate:
            direction = "downshift"
        else:
            direction = "match"
        rows.append(MaxFeasibleModeRow(lp.id, lp.mode_id, best.id, direction))
    return rows


_FAILED_SENTINEL = QoTState(
    gsnr_db=float("-inf"),
    osnr_db=float("-inf"),
    margin_db=float("-inf"),
    limiting_element_id=None,
)


@dataclass(frozen=True)
class FailureReport:
    failed_assets: Tuple[str, ...]
    downed_lightpaths: Tuple[str, ...]


def inject_failure(model: NetworkModel, asset_ids: Tuple[str, ...]) -> FailureReport:
    """Mark assets failed on the (branch) model and down every lightpath crossing them.

    Physics-free: a cut fiber / dead amp carries no signal, so crossing lightpaths
    get a failed QoT sentinel (margin = -inf). Capacity falls to 0 via the existing
    margin gate; simulate_ip_routing reports the drop.
    """
    model.mark_failed(tuple(asset_ids))
    failed = set(asset_ids)
    downed: List[str] = []
    for lp in model.list_lightpaths():
        crossing = lightpath_footprint(model, lp.oms_sequence) & failed
        if crossing:
            model.set_qot_state(lp.id, QoTState(
                gsnr_db=float("-inf"),
                osnr_db=float("-inf"),
                margin_db=float("-inf"),
                limiting_element_id=sorted(crossing)[0],
            ))
            downed.append(lp.id)
    return FailureReport(
        failed_assets=tuple(asset_ids),
        downed_lightpaths=tuple(downed),
    )


# ---------------------------------------------------------------------------
# Task 5: inject_degradation + DegradationReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DegradationRow:
    lightpath_id: str
    margin_before: float
    margin_after: float
    feasible_before: bool
    feasible_after: bool
    crossed: bool            # feasible_before and not feasible_after
    within_threshold: bool   # margin_after <= threshold_db (when threshold given)


@dataclass(frozen=True)
class DegradationReport:
    asset_id: str
    nf_delta: float
    loss_delta: float
    rows: Tuple[DegradationRow, ...]
    crossings: Tuple[str, ...]  # lightpath ids that flipped feasible -> infeasible


def _has_qot(model: NetworkModel, lp_id: str) -> bool:
    try:
        model.get_qot_state(lp_id)
        return True
    except LookupError:
        return False


def _feasible(margin_db: float) -> bool:
    """The single feasibility predicate (S8-4): mode_feasible ⇔ margin ≥ 0.

    Both feasible_before and feasible_after derive from this, so a future change
    to the feasibility rule can't silently split the two sides of a crossing.
    NaN (no baseline margin) is not feasible."""
    return margin_db >= 0


def inject_degradation(
    model: NetworkModel,
    *,
    store: QoTResultStore,
    asset_id: str,
    nf_delta: float = 0.0,
    loss_delta: float = 0.0,
    threshold_db: float = 0.0,
) -> DegradationReport:
    """Perturb impairment on a branch, recompute, report threshold crossings.

    asset_id must be a known amplifier (for nf_delta) or fiber (for loss_delta).
    Raises KeyError on an unknown asset. Margin moves as a consequence — never set.
    """
    if not nf_delta and not loss_delta:
        raise ValueError("inject_degradation needs a non-zero nf_delta or loss_delta")

    before = {lp.id: model.get_qot_state(lp.id).margin_db
              for lp in model.list_lightpaths()
              if _has_qot(model, lp.id)}

    if nf_delta:
        model.apply_nf_delta(asset_id, nf_delta)   # KeyError if not an amp
    if loss_delta:
        model.apply_loss_delta(asset_id, loss_delta)  # KeyError if not a fiber

    recompute_qot_under_loading(model=model, store=store, loading=loading_from_model(model))

    rows: List[DegradationRow] = []
    crossings: List[str] = []
    for lp in model.list_lightpaths():
        st = model.get_qot_state(lp.id)
        # S8-2: a lightpath with no feasible baseline cannot "cross" from feasible
        # to infeasible. Absent-from-before => margin_before is NaN, feasible_before
        # is False, so it is excluded from the crossing set.
        had_baseline = lp.id in before
        mb = before.get(lp.id, float("nan"))
        fb = had_baseline and _feasible(mb)
        fa = _feasible(st.margin_db)
        crossed = fb and not fa
        if crossed:
            crossings.append(lp.id)
        rows.append(DegradationRow(
            lightpath_id=lp.id, margin_before=mb, margin_after=st.margin_db,
            feasible_before=fb, feasible_after=fa, crossed=crossed,
            within_threshold=st.margin_db <= threshold_db))
    return DegradationReport(asset_id=asset_id, nf_delta=nf_delta, loss_delta=loss_delta,
                             rows=tuple(rows), crossings=tuple(crossings))


# ---------------------------------------------------------------------------
# whatif_sensitivity: per-asset QoT sensitivity via branch diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetSensitivityRow:
    element_id: str
    gsnr_contribution_delta_db: float
    ase_contribution_delta_db: float
    nli_contribution_delta_db: float


@dataclass(frozen=True)
class SensitivityResult:
    delta_margin_db: float
    delta_gsnr_db: float
    rows: Tuple[AssetSensitivityRow, ...]   # sorted by |gsnr_contribution_delta_db| desc


def _safe_delta(before: float, after: float) -> float:
    """after - before, but treats "equal, including both non-finite" as exactly
    0.0. ElementSnapshot.gsnr_delta_db is structurally +-inf/NaN for elements at
    or before the path's first noise-introducing point (the adapter's own
    prev_gsnr_db starts at +inf) regardless of what changed elsewhere on the
    path — a bare subtraction of two such values (e.g. -inf - -inf) yields NaN
    even when nothing about that element actually differs between branches,
    which would corrupt the |delta| sort. A genuine finite<->non-finite
    transition (a real signal: this element newly introduces/stops introducing
    measurable noise) still surfaces as a real (possibly infinite) delta."""
    if before == after:                             # covers inf == inf
        return 0.0
    if math.isnan(before) and math.isnan(after):
        return 0.0
    return after - before


def whatif_sensitivity(
    model_a: NetworkModel, model_b: NetworkModel, *,
    store: QoTResultStore,
    oms_sequence: Tuple[str, ...], direction, mode_id: str,
    loading: LoadingState,
) -> SensitivityResult:
    """Diff per-element QoT contribution between two branches (typically a
    nominal baseline and one with inject_degradation applied) for the SAME
    path/direction/mode/loading — isolates which asset's OWN contribution
    changed, not the cumulative gsnr_db_after figure (which shifts at every
    element downstream of the real cause too, echoing it rather than isolating
    it). Read-only: computes QoT fresh on each model, mutates neither. Both
    result_ids remain retrievable afterward via get_qot_breakdown (same
    `store` for both calls).

    Task 14: a naive diff of model_a's and model_b's own per-element
    gsnr_delta_db (each already a marginal, hop-to-hop figure) still leaks —
    every element AFTER the real perturbation receives a different *input* SI
    in model_b's propagation than in model_a's, even when that downstream
    element's own physical parameters are unchanged, so its own marginal delta
    shifts too (confirmed empirically: a +4 dB NF bump on one amp showed the
    correct -1.53 dB there, but also a leaked +0.29 dB / +0.19 dB at untouched
    downstream amps). Per-row isolation fixes this: for each element position,
    `isolate_element_contribution` replays model_a's OWN trace up to that
    position, then substitutes ONLY model_b's version of that one element, and
    the row compares model_a's baseline delta at that position to THIS
    isolated delta — not to model_b's own (leaky) cumulative trace value.

    Known blind spot (pre-existing, not introduced by Task 14 — pre-fix, this
    case misattributed the delta to the wrong element; post-fix nothing is
    misattributed, it is simply silent): a perturbation whose only effect is at
    the very first noise-introducing element in the chain (e.g. a booster amp
    right after the launch transceiver) can show `gsnr_contribution_delta_db`
    as 0.0 on EVERY row, even though `delta_gsnr_db` (the whole-path summary
    above) is nonzero. That element's `gsnr_delta_db` is `-inf` in both
    branches (the adapter's own `prev_gsnr_db` starts at `+inf`), and
    `_safe_delta`'s `-inf == -inf -> 0.0` handling collapses the row. When this
    happens, `ase_contribution_delta_db` / `nli_contribution_delta_db` on the
    same row still correctly pinpoint the perturbed element (both are plain
    finite-value deltas, not gated by the same infinity collapse).

    Sign/decomposition caveat: rows localize which element's OWN parameters
    changed between the two branches — they are not a decomposition of
    `delta_gsnr_db` and do not sum or reconcile to it, and a given row's sign
    can differ from `delta_gsnr_db`'s sign. This is visible for a pure loss
    injection: attenuation itself is GSNR-neutral at the point of injection, so
    the actual GSNR damage from a loss delta shows up as a downstream
    element's own (physically unchanged) parameters interacting with a
    different incoming signal level, which can localize to a row whose sign
    reads positive even though the overall change is a degradation."""
    state_a, rid_a = compute_qot(model=model_a, store=store, oms_sequence=oms_sequence,
                                 direction=direction, mode_id=mode_id, loading=loading)
    state_b, rid_b = compute_qot(model=model_b, store=store, oms_sequence=oms_sequence,
                                 direction=direction, mode_id=mode_id, loading=loading)
    snapshots_a = store.get(rid_a).snapshots
    by_id_b = {s.element_id: s for s in store.get(rid_b).snapshots}
    rows = []
    for position, snap_a in enumerate(snapshots_a):
        eid = snap_a.element_id
        if eid not in by_id_b:
            continue
        isolated = isolate_element_contribution(
            model_a=model_a, model_b=model_b, oms_sequence=oms_sequence,
            direction=direction, mode_id=mode_id, loading=loading, position=position)
        assert isolated.element_id == eid, (
            f"isolate_element_contribution resolved position {position} to "
            f"element {isolated.element_id!r}, but the main propagation "
            f"(compute_qot on model_a) resolved the same position to {eid!r}. "
            "isolate_element_contribution independently re-resolves the path "
            "via _resolve_unpropagated_path rather than reusing compute_qot's "
            "resolution; this guards against the two paths silently drifting."
        )
        rows.append(AssetSensitivityRow(
            element_id=eid,
            gsnr_contribution_delta_db=_safe_delta(snap_a.gsnr_delta_db,
                                                    isolated.gsnr_delta_db),
            ase_contribution_delta_db=_safe_delta(snap_a.ase_contribution_db,
                                                   isolated.ase_contribution_db),
            nli_contribution_delta_db=_safe_delta(snap_a.nli_contribution_db,
                                                   isolated.nli_contribution_db),
        ))
    rows.sort(key=lambda r: abs(r.gsnr_contribution_delta_db), reverse=True)
    return SensitivityResult(
        delta_margin_db=state_b.margin_db - state_a.margin_db,
        delta_gsnr_db=state_b.gsnr_db - state_a.gsnr_db,
        rows=tuple(rows),
    )
