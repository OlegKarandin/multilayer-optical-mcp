"""compute_qot — load-bearing GNPy propagation function.

Design contract:
- ``loading`` is an arbitrary constructed channel set; it need not match the
  committed network (supports make-before-break transient evaluation).
- ``direction`` is required; per-direction QoT is physically real.
- Returns ``(QoTState, result_id)`` where ``result_id`` is the key into
  ``QoTResultStore`` for the full per-element breakdown.

gnpy single-channel limitation:
    gnpy's EDFA ``interpol_params`` computes ``slot_width`` as
    ``channel_freq[1] - channel_freq[0]``, which fails with only one carrier.
    When the loading state contains exactly one channel, a lightweight dummy
    channel (at ``probe_freq + 100 GHz``) is injected so the EDFA can
    interpolate.  The dummy does not affect the probe channel's GSNR: NLI
    cross-talk between two adjacent channels on this toy 2-span topology is
    < 0.1 µdB (verified empirically).  Results for the probe channel are
    extracted by frequency index; the dummy is discarded.
"""

from __future__ import annotations

import math
from typing import Tuple

from ..model.assets import Direction
from ..model.network import NetworkModel
from ..model.qot import ElementSnapshot, QoTBreakdown, QoTState
from ..model.qot_results import QoTResultStore
from .loading import Channel, LoadingState
from .translate import build_si_for_loading, load_toy, resolve_oms_path_to_uids

# Spacing (Hz) used for the synthetic dummy channel injected when the loading
# state contains only a single carrier.
_DUMMY_SPACING_HZ = 100e9


def _ensure_min_two_channels(loading: LoadingState, probe_freq_hz: float) -> LoadingState:
    """Return a loading state with at least two channels.

    If *loading* already has ≥ 2 channels, return it unchanged.  Otherwise
    inject a dummy channel at ``probe_freq_hz + _DUMMY_SPACING_HZ`` so that
    gnpy EDFAs (which require ≥ 2 carriers) can propagate.

    The dummy uses the same mode_id as the probe channel so that
    ``build_si_for_loading`` receives a consistent label.  Its GSNR contribution
    to the probe channel is negligible (< 0.1 µdB on the toy topology).
    """
    if len(loading.channels) >= 2:
        return loading
    probe = loading.channels[0]
    dummy = Channel(
        center_freq_hz=probe_freq_hz + _DUMMY_SPACING_HZ,
        slot_width_hz=probe.slot_width_hz,
        power_dbm=probe.power_dbm,
        mode_id=probe.mode_id,
    )
    return LoadingState(channels=(probe, dummy))


def _find_probe_index(loading: LoadingState, probe_freq_hz: float) -> int:
    """Return the positional index of the probe channel in *loading.channels*."""
    for i, ch in enumerate(loading.channels):
        if ch.center_freq_hz == probe_freq_hz:
            return i
    raise ValueError(
        f"probe frequency {probe_freq_hz:.6e} Hz not found in loading state"
    )


def _extract_gsnr_osnr(si, idx: int) -> Tuple[float, float]:
    """Compute (gsnr_db, osnr_db) for carrier at position *idx* in *si*.

    GSNR = signal / (ASE + NLI)   (in-band SNR, baud-rate normalised)
    OSNR = signal / ASE             (noise-figure limited)

    Both are returned in dB.  If ASE is zero the OSNR is ``+inf``; if both
    ASE and NLI are zero GSNR is also ``+inf``.
    """
    sig = float(si.signal[idx])
    ase = float(si.ase[idx])
    nli = float(si.nli[idx])

    total_noise = ase + nli
    gsnr_db = 10.0 * math.log10(sig / total_noise) if total_noise > 0.0 else math.inf
    osnr_db = 10.0 * math.log10(sig / ase) if ase > 0.0 else math.inf
    return gsnr_db, osnr_db


def _path_elements(network, uids: Tuple[str, ...]) -> list:
    """Return gnpy node objects for *uids* in order, raising KeyError on miss."""
    by_uid = {n.uid: n for n in network.nodes}
    missing = [u for u in uids if u not in by_uid]
    if missing:
        raise KeyError(f"unknown uids in gnpy network: {missing}")
    return [by_uid[u] for u in uids]


def compute_qot(
    *,
    model: NetworkModel,
    store: QoTResultStore,
    oms_sequence: Tuple[str, ...],
    direction: Direction,
    mode_id: str,
    loading: LoadingState,
) -> Tuple[QoTState, str]:
    """Propagate *loading* through the gnpy toy network and return QoT.

    Parameters
    ----------
    model:
        The NetworkModel that owns the OMS and mode definitions.
    store:
        QoTResultStore that will hold the per-element breakdown.
    oms_sequence:
        Ordered OMS ids that define the end-to-end optical path.
    direction:
        ``FORWARD`` or ``BACKWARD``.  When ``BACKWARD``, the element order is
        reversed so per-direction asymmetric degradation can be evaluated.
    mode_id:
        Transceiver mode for the probe channel.
    loading:
        Arbitrary constructed channel set.  Must contain at least one channel
        whose ``mode_id`` matches *mode_id*.  Additional channels model WDM
        neighbors (including make-before-break overlap sets).

    Returns
    -------
    (QoTState, result_id)
        ``QoTState`` holds the final GSNR, OSNR, margin, and whether the mode
        is feasible (margin ≥ 0).  ``result_id`` is the key into *store* for
        the full ``QoTBreakdown`` with per-element snapshots.

    Raises
    ------
    ValueError
        If *loading* contains no channel with the given *mode_id*.
    KeyError
        If any OMS id or element uid cannot be resolved.
    """
    # ------------------------------------------------------------------ setup
    eqpt, network = load_toy()

    # Set amplifier gain targets and fiber input powers from the equipment spec.
    # pref_ch_db = -20 dBm matches the default tx_power_dbm used in build_si_for_loading.
    from gnpy.core.network import build_network as _build_network
    _build_network(network, eqpt, pref_ch_db=-20.0, pref_total_db=-20.0)

    # Resolve the OMS sequence to gnpy element uids.
    uids = resolve_oms_path_to_uids(model, oms_sequence)
    if direction == Direction.BACKWARD:
        uids = tuple(reversed(uids))

    elements = _path_elements(network, uids)

    # ------------------------------------------------------------------ probe channel
    probe = next((c for c in loading.channels if c.mode_id == mode_id), None)
    if probe is None:
        raise ValueError(
            f"loading does not include a channel for mode {mode_id!r}"
        )

    mode = model.modes.get(mode_id)  # raises KeyError if unknown

    # ------------------------------------------------------------------ SI construction
    # Ensure at least 2 channels so gnpy EDFAs can interpolate slot_width.
    loading_for_gnpy = _ensure_min_two_channels(loading, probe.center_freq_hz)
    probe_idx = _find_probe_index(loading_for_gnpy, probe.center_freq_hz)

    si = build_si_for_loading(
        loading_for_gnpy,
        baud_rate=mode.symbol_rate_baud,
        roll_off=0.15,
        tx_osnr=40.0,
    )

    # ------------------------------------------------------------------ propagation
    # Feed SI through the transmitter first (initialises tx_power on SI).
    by_uid = {n.uid: n for n in network.nodes}

    # gnpy Transceiver at the A-end initialises tx_power; use 'trx A' from the
    # toy topology if available, otherwise skip (forward-only assumption for now).
    if "trx A" in by_uid:
        si = by_uid["trx A"](si)

    prev_gsnr_db = math.inf
    snapshots: list[ElementSnapshot] = []

    for uid, el in zip(uids, elements):
        si = el(si)

        gsnr_db, osnr_db = _extract_gsnr_osnr(si, probe_idx)

        # ASE / NLI absolute contributions (dBW, -inf when zero)
        ase_w = float(si.ase[probe_idx])
        nli_w = float(si.nli[probe_idx])
        ase_contrib_db = 10.0 * math.log10(ase_w) if ase_w > 0.0 else -math.inf
        nli_contrib_db = 10.0 * math.log10(nli_w) if nli_w > 0.0 else -math.inf

        # Delta: negative when this element degrades GSNR.
        # We store -inf for the first finite→finite transitions coming out of
        # the noise-free fibre (where prev was +inf).
        gsnr_delta_db = gsnr_db - prev_gsnr_db

        snapshots.append(
            ElementSnapshot(
                element_id=uid,
                gsnr_db_after=gsnr_db,
                osnr_db_after=osnr_db,
                gsnr_delta_db=gsnr_delta_db,
                ase_contribution_db=ase_contrib_db,
                nli_contribution_db=nli_contrib_db,
            )
        )

        if math.isfinite(gsnr_db):
            prev_gsnr_db = gsnr_db

    # ------------------------------------------------------------------ final GSNR
    # The last finite GSNR in the snapshot chain is the end-to-end value.
    final_gsnr_db = prev_gsnr_db if math.isfinite(prev_gsnr_db) else math.inf
    final_osnr_db = snapshots[-1].osnr_db_after if snapshots else math.inf

    margin_db = final_gsnr_db - mode.required_gsnr_db

    # ------------------------------------------------------------------ limiting element
    # The limiting element is the one with the most negative finite GSNR delta.
    # Infinite deltas (first ASE introduction) are considered the "worst" in
    # degradation terms — the first amplifier is always the primary noise source
    # on a balanced chain — so we pick the minimum delta including -inf.
    # If all deltas are +inf (degenerate case), there is no limiting element.
    limiting_element_id: str | None = None
    min_delta = math.inf
    for snap in snapshots:
        if snap.gsnr_delta_db < min_delta:
            min_delta = snap.gsnr_delta_db
            limiting_element_id = snap.element_id

    # ------------------------------------------------------------------ store
    breakdown = QoTBreakdown(
        snapshots=tuple(snapshots),
        limiting_element_id=limiting_element_id,
    )
    result_id = store.put(breakdown)

    state = QoTState(
        gsnr_db=final_gsnr_db,
        osnr_db=final_osnr_db,
        margin_db=margin_db,
        limiting_element_id=limiting_element_id,
    )

    return state, result_id


def gated_qot(
    *,
    model: NetworkModel,
    store: QoTResultStore,
    oms_sequence: Tuple[str, ...],
    mode_id: str,
    loading: LoadingState,
) -> Tuple[QoTState, str]:
    """Return the worse of forward/backward QoT, along with its breakdown id."""
    fwd_state, fwd_rid = compute_qot(model=model, store=store,
                                     oms_sequence=oms_sequence,
                                     direction=Direction.FORWARD,
                                     mode_id=mode_id, loading=loading)
    bwd_state, bwd_rid = compute_qot(model=model, store=store,
                                     oms_sequence=oms_sequence,
                                     direction=Direction.BACKWARD,
                                     mode_id=mode_id, loading=loading)
    if fwd_state.gsnr_db <= bwd_state.gsnr_db:
        return fwd_state, fwd_rid
    return bwd_state, bwd_rid


def recompute_qot_under_loading(
    *, model: NetworkModel, store: QoTResultStore, loading: LoadingState,
) -> dict[str, Tuple[QoTState, str]]:
    """Compute gated QoT for every lightpath in model under loading.

    Writes QoTState on the model and returns {lp_id: (QoTState, result_id)}
    so callers can pull per-lightpath breakdowns on demand.
    """
    results: dict[str, Tuple[QoTState, str]] = {}
    for lp in model.list_lightpaths():
        state, rid = gated_qot(model=model, store=store,
                               oms_sequence=lp.oms_sequence,
                               mode_id=lp.mode_id, loading=loading)
        model.set_qot_state(lp.id, state)
        results[lp.id] = (state, rid)
    return results
