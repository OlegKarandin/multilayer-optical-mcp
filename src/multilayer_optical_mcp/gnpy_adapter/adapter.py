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
from pathlib import Path
from typing import Optional, Tuple

from ..model.assets import Direction
from ..model.network import NetworkModel
from ..model.qot import ElementSnapshot, QoTBreakdown, QoTState
from ..model.qot_results import QoTResultStore
from .loading import Channel, LoadingState
from .translate import (
    DEFAULT_EQPT,
    DEFAULT_TOPO,
    build_si_for_loading,
    load_toy,
    resolve_oms_path_to_uids,
    reverse_oms_sequence,
)

# Spacing (Hz) used for the synthetic dummy channel injected when the loading
# state contains only a single carrier.
_DUMMY_SPACING_HZ = 100e9

# SI band upper edge (Hz). Mirrors the SI ``f_max`` synthesize.py builds
# (191.3–196.1 THz) and the default spectrum grid's top slot (196.1 THz). A
# single-channel dummy must stay within this edge; near the top of the band the
# default ``probe + 100 GHz`` placement would land out of band (S4-1/A6).
_SI_F_MAX_HZ = 196.1e12


def _ensure_min_two_channels(loading: LoadingState, probe_freq_hz: float) -> LoadingState:
    """Return a loading state with at least two channels.

    If *loading* already has ≥ 2 channels, return it unchanged.  Otherwise
    inject a dummy channel one ``_DUMMY_SPACING_HZ`` step away from the probe so
    that gnpy EDFAs (which require ≥ 2 carriers) can propagate.

    The dummy is placed *above* the probe by default, but *below* it when the
    above position would exceed the SI band edge (``_SI_F_MAX_HZ``) — otherwise a
    probe near the top of the band would push the dummy out of band (S4-1/A6).
    The returned channels are frequency-ascending so the frequency-resolved probe
    index still aligns with gnpy's (frequency-ordered) SI arrays.

    The dummy uses the same mode_id as the probe channel so that
    ``build_si_for_loading`` receives a consistent label.  Its GSNR contribution
    to the probe channel is negligible (< 0.1 µdB on the toy topology).
    """
    if len(loading.channels) >= 2:
        return loading
    probe = loading.channels[0]
    above = probe_freq_hz + _DUMMY_SPACING_HZ
    if above <= _SI_F_MAX_HZ:
        dummy_freq_hz, ordered = above, (probe,)  # probe first (ascending)
    else:
        dummy_freq_hz, ordered = probe_freq_hz - _DUMMY_SPACING_HZ, None
    dummy = Channel(
        center_freq_hz=dummy_freq_hz,
        slot_width_hz=probe.slot_width_hz,
        power_dbm=probe.power_dbm,
        mode_id=probe.mode_id,
    )
    # Keep channels frequency-ascending: dummy below → dummy first, else probe first.
    channels = (dummy, probe) if ordered is None else (probe, dummy)
    return LoadingState(channels=channels)


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


def _find_launch_transceiver(network, path_uids, by_uid):
    """Return the GNPy Transceiver feeding the first path element, or None.

    Generic replacement for the hard-coded 'trx A': the launch transceiver is a
    Transceiver predecessor of path_uids[0] in the GNPy graph.
    """
    from gnpy.core.elements import Transceiver as _GnpyTrx
    first = by_uid.get(path_uids[0])
    if first is None:
        return None
    for pred in network.predecessors(first):
        if isinstance(pred, _GnpyTrx):
            return pred
    return None


def _trx_neighbor_uid(network, node, *, predecessor: bool):
    """UID of node's Transceiver predecessor (add port) or successor (drop port)."""
    from gnpy.core.elements import Transceiver as _GnpyTrx
    it = network.predecessors(node) if predecessor else network.successors(node)
    for x in it:
        if isinstance(x, _GnpyTrx):
            return x.uid
    return None


def _roadm_successor(network, node):
    """The Roadm successor of *node* in the GNPy graph, or None.

    A path's terminal OMS ends at its last amplifier; the physically adjacent
    drop ROADM is that amplifier's Roadm successor. It is nobody's OMS chain[0],
    so callers must append it explicitly to apply its drop-side penalty (S4-4).
    """
    from gnpy.core.elements import Roadm as _GnpyRoadm
    for x in network.successors(node):
        if isinstance(x, _GnpyRoadm):
            return x
    return None


def compute_qot(
    *,
    model: NetworkModel,
    store: QoTResultStore,
    oms_sequence: Tuple[str, ...],
    direction: Direction,
    mode_id: str,
    loading: LoadingState,
    center_freq_hz: Optional[float] = None,
    topo_path: Optional[Path] = None,
    eqpt_path: Optional[Path] = None,
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
    from .synthesize import build_gnpy_network, gnpy_design_network
    if topo_path is not None or eqpt_path is not None:
        eqpt, network = load_toy(eqpt_path=eqpt_path or DEFAULT_EQPT,
                                 topo_path=topo_path or DEFAULT_TOPO)
        gnpy_design_network(network, eqpt)
    else:
        eqpt, network = build_gnpy_network(model)

    # Resolve the OMS sequence to gnpy element uids.
    # BACKWARD walks the physically separate reverse OMS chain (its own amps and
    # ROADM add-side penalty) in natural travel order — never a reversed forward
    # element list, which would walk the *forward* fiber's amps in reverse and
    # silently discard asymmetric per-direction degradation (S4-2/S4-3).
    if direction == Direction.BACKWARD:
        rev_seq = reverse_oms_sequence(model, oms_sequence)
        if rev_seq is None:
            raise ValueError(
                f"backward QoT requires a paired reverse OMS for every leg of "
                f"{oms_sequence!r}, but none was found. Build the model via the "
                f"importer (model_from_abstract_graph) or register reverse-"
                f"direction OMS so each (src,dst) has a (dst,src) counterpart."
            )
        uids = resolve_oms_path_to_uids(model, rev_seq)
    else:
        uids = resolve_oms_path_to_uids(model, oms_sequence)

    elements = _path_elements(network, uids)

    # S4-4: the terminal drop ROADM is nobody's OMS chain[0], so it is never in
    # the resolved element list. Append it (the last element's Roadm successor)
    # so its drop-side add_drop_osnr penalty is applied — forward and backward.
    drop_roadm = _roadm_successor(network, elements[-1]) if elements else None
    if drop_roadm is not None and drop_roadm.uid not in uids:
        uids = uids + (drop_roadm.uid,)
        elements = elements + [drop_roadm]

    # ------------------------------------------------------------------ probe channel
    # Prefer selecting the probe by frequency: in a WDM load with several
    # same-mode channels, mode_id alone is ambiguous and every same-mode
    # lightpath would be evaluated at the first match's frequency (S4-5). Fall
    # back to mode_id only when no frequency is given (single-channel/legacy).
    if center_freq_hz is not None:
        probe = next(
            (c for c in loading.channels if c.center_freq_hz == center_freq_hz), None
        )
        if probe is None:
            raise ValueError(
                f"loading has no channel at center_freq {center_freq_hz:.6e} Hz"
            )
    else:
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
    )

    # ------------------------------------------------------------------ propagation
    # Feed SI through the transmitter first (initialises tx_power on SI).
    by_uid = {n.uid: n for n in network.nodes}
    uids_list = list(uids)

    # gnpy Transceiver at the A-end initialises tx_power; find it generically
    # as a Transceiver predecessor of the first path element.
    launch_trx = _find_launch_transceiver(network, uids_list, by_uid)
    if launch_trx is not None:
        si = launch_trx(si)

    # ROADM.__call__ needs degree and from_degree as positional args (resolved
    # per element inside the loop from the walk order + transceiver neighbors).
    from gnpy.core.elements import Roadm as _GnpyRoadm

    prev_gsnr_db = math.inf
    snapshots: list[ElementSnapshot] = []
    _roadm_propagated: set[str] = set()
    for i, (uid, el) in enumerate(zip(uids_list, elements)):
        if isinstance(el, _GnpyRoadm):
            # A ROADM's from/to degree is its previous/next element on the walk,
            # except at the ends where the peer is the add/drop transceiver:
            #   - source (add) ROADM:   from_degree = add transceiver
            #   - express ROADM:        from/to = adjacent path elements
            #   - terminal (drop) ROADM: to_degree = drop transceiver
            from_uid = (
                uids_list[i - 1] if i > 0
                else _trx_neighbor_uid(network, el, predecessor=True)
            )
            to_uid = (
                uids_list[i + 1] if i < len(uids_list) - 1
                else _trx_neighbor_uid(network, el, predecessor=False)
            )
            # Only propagate if this from→to path is registered in the gnpy graph.
            if (from_uid is not None and to_uid is not None
                    and any(rp.from_degree == from_uid and rp.to_degree == to_uid
                            for rp in el.roadm_paths)):
                si = el(si, degree=to_uid, from_degree=from_uid)
                _roadm_propagated.add(uid)
        else:
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

    # ------------------------------------------------------------------ post-propagation penalties
    # gnpy does not apply add_drop_osnr or tx_osnr to si.ase during bare element propagation;
    # they are metadata consumed only by request.py's Transceiver.update_snr(). We apply them
    # here using gnpy's own snr_sum, which normalises each penalty from 12.5 GHz to baud_rate.
    from gnpy.core.utils import snr_sum as _snr_sum, lin2db as _lin2db, db2lin as _db2lin

    baud_rate = mode.symbol_rate_baud

    penalties_noise_lin = 0.0
    for _uid, _el in zip(uids_list, elements):
        if isinstance(_el, _GnpyRoadm) and _uid in _roadm_propagated:
            penalties_noise_lin += _db2lin(-_el.params.add_drop_osnr)

    tx_osnr_db = float(si.tx_osnr[probe_idx])  # at 12.5 GHz ref BW
    penalties_noise_lin += _db2lin(-tx_osnr_db)

    if penalties_noise_lin > 0.0:
        combined_penalty_db = -_lin2db(penalties_noise_lin)
        final_gsnr_db = float(_snr_sum(final_gsnr_db, baud_rate, combined_penalty_db))
        final_osnr_db = float(_snr_sum(final_osnr_db, baud_rate, combined_penalty_db))

    margin_db = final_gsnr_db - mode.required_gsnr_db

    # ------------------------------------------------------------------ limiting element
    # The limiting element is the one with the most negative *finite* GSNR delta —
    # the largest real degradation along the chain. Non-finite deltas are excluded
    # (S4-7/A5): the first noise-introducing element (booster) transitions the
    # probe from the noise-free +inf regime to a finite GSNR, giving a spurious
    # ``finite - inf = -inf`` delta that would otherwise always win this min and
    # make the diagnostic meaningless. Elements before any noise (+inf → +inf give
    # NaN) are likewise skipped. If no finite delta exists, there is none.
    limiting_element_id: str | None = None
    min_delta = math.inf
    for snap in snapshots:
        if math.isfinite(snap.gsnr_delta_db) and snap.gsnr_delta_db < min_delta:
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
    center_freq_hz: Optional[float] = None,
) -> Tuple[QoTState, str]:
    """Return the worse of forward/backward QoT, along with its breakdown id."""
    fwd_state, fwd_rid = compute_qot(model=model, store=store,
                                     oms_sequence=oms_sequence,
                                     direction=Direction.FORWARD,
                                     mode_id=mode_id, loading=loading,
                                     center_freq_hz=center_freq_hz)
    bwd_state, bwd_rid = compute_qot(model=model, store=store,
                                     oms_sequence=oms_sequence,
                                     direction=Direction.BACKWARD,
                                     mode_id=mode_id, loading=loading,
                                     center_freq_hz=center_freq_hz)
    if fwd_state.gsnr_db <= bwd_state.gsnr_db:
        return fwd_state, fwd_rid
    return bwd_state, bwd_rid


def _per_path_loading(grid, occ_mask: int, probe_slot: int, mode_id: str) -> LoadingState:
    """Probe channel at *probe_slot* plus the channels lit on this path's own OMS
    (``occ_mask`` = the path's per-OMS occupancy already restricted to lit slots).

    Mirrors ``allocation._build_loading``: interferers come from the path's OMS
    occupancy, not a global concat, so channels on disjoint fibers never appear
    and a reused wavelength collapses to one grid slot (no duplicate carrier)."""
    # Channels must be frequency-sorted: gnpy orders SpectralInformation carriers
    # by frequency, so the probe index (resolved by frequency) only aligns with
    # the SI arrays when the loading is already ascending.
    slots = sorted({probe_slot} | {s for s in range(grid.num_slots)
                                   if (occ_mask >> s) & 1})
    channels = tuple(
        Channel(grid.freq(s), grid.spacing_hz, None, mode_id) for s in slots
    )
    return LoadingState(channels)


def recompute_qot_under_loading(
    *, model: NetworkModel, store: QoTResultStore, loading: LoadingState,
) -> dict[str, Tuple[QoTState, str]]:
    """Compute gated QoT for every lightpath in model under *loading*.

    Each lightpath's interferer comb is built from *its own* OMS occupancy (the
    per-OMS spectrum bitmask), intersected with the slots lit by *loading*. This
    replaces the old global concat that propagated every committed channel
    through every path (NLI over-count on disjoint fibers) and emitted duplicate
    same-frequency carriers for wavelength reuse (malformed NLI) — S4-6/S8-5.

    Writes QoTState on the model and returns {lp_id: (QoTState, result_id)}.
    """
    from ..model.spectrum import SpectrumGrid, build_spectrum_state, occupied_along
    from ..model.exposure import lightpath_footprint

    grid = SpectrumGrid.default()
    model_state = build_spectrum_state(model, grid)
    # S8-1: a lightpath crossing a failed asset stays down. Recompute must NOT
    # overwrite inject_failure's -inf sentinel with a feasible GSNR (synthesis
    # ignores _failed_assets, so the cut fiber still propagates a signal). Re-apply
    # the sentinel using the same footprint predicate inject_failure uses.
    failed = model.failed_assets()
    # Slots lit by the passed loading (a make-before-break drop removes a channel
    # as an interferer; duplicate frequencies collapse to one slot bit).
    lit = 0
    for ch in loading.channels:
        try:
            lit |= 1 << grid.slot_of(ch.center_freq_hz)
        except ValueError:
            continue  # off-grid channel contributes no grid interferer

    results: dict[str, Tuple[QoTState, str]] = {}
    for lp in model.list_lightpaths():
        if failed:
            crossing = lightpath_footprint(model, lp.oms_sequence) & failed
            if crossing:
                marker = sorted(crossing)[0]
                sentinel = QoTState(gsnr_db=float("-inf"), osnr_db=float("-inf"),
                                    margin_db=float("-inf"), limiting_element_id=marker)
                rid = store.put(QoTBreakdown(snapshots=(), limiting_element_id=marker))
                model.set_qot_state(lp.id, sentinel)
                results[lp.id] = (sentinel, rid)
                continue
        probe_slot = grid.slot_of(lp.center_freq_hz)
        occ = occupied_along(model_state, lp.oms_sequence) & lit
        per_path = _per_path_loading(grid, occ, probe_slot, lp.mode_id)
        state, rid = gated_qot(model=model, store=store,
                               oms_sequence=lp.oms_sequence,
                               mode_id=lp.mode_id, loading=per_path,
                               center_freq_hz=grid.freq(probe_slot))
        model.set_qot_state(lp.id, state)
        results[lp.id] = (state, rid)
    return results
