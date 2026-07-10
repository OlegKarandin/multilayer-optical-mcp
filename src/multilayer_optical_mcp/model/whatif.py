"""Branch-scoped what-if + injection.

Margin is an OUTPUT: the sweep screens recorded margin; inject_degradation
perturbs a physical parameter and lets margin move via recompute. None of these
mutate ground truth — callers pass a branch model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from ..gnpy_adapter.loading import Channel, LoadingState
from ..gnpy_adapter.adapter import recompute_qot_under_loading
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
    """One channel per committed lightpath at its center frequency."""
    channels = []
    for lp in model.list_lightpaths():
        mode = model.modes.get(lp.mode_id)
        channels.append(Channel(
            center_freq_hz=lp.center_freq_hz,
            slot_width_hz=mode.channel_spacing_hz,
            power_dbm=None,  # S2-2: use the adapter tx_power default
            mode_id=lp.mode_id,
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
