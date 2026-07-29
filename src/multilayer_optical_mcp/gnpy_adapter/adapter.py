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
from typing import NamedTuple, Optional, Tuple

from ..model.assets import Direction
from ..model.network import NetworkModel
from ..model.qot import ElementSnapshot, QoTBreakdown, QoTState
from ..model.qot_results import QoTResultStore, QoTCache
from .bands import SI_BAND
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

# SI band upper edge (Hz). Sourced from the single canonical SI band (bands.py,
# 191.3–196.1 THz), which synthesize.py also builds the SI config from, so the two
# can no longer drift (S3-10). A single-channel dummy must stay within this edge;
# near the top of the band the default ``probe + 100 GHz`` placement would land
# out of band (S4-1/A6).
_SI_F_MAX_HZ = SI_BAND.f_max_hz


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


def _path_physical_fingerprint(
    model: NetworkModel, oms_sequence: Tuple[str, ...], direction: Direction,
) -> tuple:
    """Fingerprint every GSNR-relevant physical input on the resolved path.

    The asset dataclasses (Amplifier/Fiber/FiberType/ROADM) are frozen and
    hashable, so each element object is embedded whole — any field change (an
    ``inject_degradation`` NF/loss delta, a different length or gain) flips the
    fingerprint. Path-scoped, so a degradation on a disjoint OMS leaves this
    path's key unchanged (content-addressing, no explicit invalidation)."""
    if direction == Direction.BACKWARD:
        seq = reverse_oms_sequence(model, oms_sequence)
        if seq is None:                      # unpaired reverse OMS: compute_qot raises later
            seq = oms_sequence
    else:
        seq = oms_sequence
    parts: list = []
    for oms_id in seq:
        oms = model.get_oms(oms_id)          # KeyError => caller handles as a miss
        parts.append(("oms", oms_id, oms.src_node_id, oms.dst_node_id))
        for el_id in oms.elements:
            if el_id in model._amplifiers:
                parts.append(model._amplifiers[el_id])
            elif el_id in model._fibers:
                f = model._fibers[el_id]
                parts.append(f)
                parts.append(model.get_fiber_type(f.type_variety))
            elif el_id in model._roadms:
                parts.append(model._roadms[el_id])
            else:
                parts.append(("el", el_id))
    # S4-4 terminal drop ROADM (appended in compute_qot; constant params, embedded
    # for completeness so a differing drop ROADM keys distinctly).
    if seq:
        drop_id = f"roadm_{model.get_oms(seq[-1]).dst_node_id}"
        if drop_id in model._roadms:
            parts.append(model._roadms[drop_id])
    return tuple(parts)


def _cache_key(
    model: NetworkModel, oms_sequence: Tuple[str, ...], direction: Direction,
    mode_id: str, loading: LoadingState, center_freq_hz: Optional[float],
) -> tuple:
    """Full content-addressed key: path physical params + loading + direction +
    mode + probe frequency — every input that determines the returned GSNR."""
    return (
        tuple(oms_sequence),
        direction.value,
        mode_id,
        center_freq_hz,
        loading.channels,                    # frozen Channels: freq/width/mode/baud
        _path_physical_fingerprint(model, oms_sequence, direction),
    )


class _PropResult(NamedTuple):
    """Result of propagating a loading state through every path element.

    Shared shape returned by ``_propagate_loading``, consumed by ``compute_qot``
    (one probe) and, later, ``harvest_qot`` (all slots in one pass)."""
    si: object
    uids_list: list
    elements: list
    roadm_propagated: set
    baud_rate: float
    snapshots: list
    final_gsnr_db: float
    final_osnr_db: float


def _propagate_loading(
    model: NetworkModel,
    oms_sequence: Tuple[str, ...],
    direction: Direction,
    loading_for_gnpy: LoadingState,
    mode,
    probe_idx: int,
    *,
    topo_path: Optional[Path] = None,
    eqpt_path: Optional[Path] = None,
) -> "_PropResult":
    """Resolve the path, build the SI from *loading_for_gnpy*, propagate through
    every element, and return the final SI plus per-element snapshots taken at
    *probe_idx*. Shared by compute_qot (one probe) and harvest_qot (all slots)."""
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

    # ------------------------------------------------------------------ SI construction
    si = build_si_for_loading(
        loading_for_gnpy,
        baud_rate=mode.symbol_rate_baud,
        roll_off=mode.roll_off,
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

    return _PropResult(si, uids_list, elements, _roadm_propagated,
                       mode.symbol_rate_baud, snapshots, final_gsnr_db, final_osnr_db)


def _apply_penalties(si, idx, uids_list, elements, roadm_propagated, baud_rate,
                     gsnr_db, osnr_db) -> tuple[float, float]:
    """Apply add_drop_osnr (terminal ROADMs only, half-budget corrected) +
    tx_osnr (per carrier idx), normalised from 12.5 GHz to baud_rate via gnpy's
    snr_sum. Mirrors the block formerly inline in compute_qot.

    gnpy's `add_drop_osnr` is the COMBINED add+drop noise budget for a full
    add+drop cycle. A TERMINAL ROADM (the first or last propagated element on
    the path) incurs only one side of that (either the add or the drop, never
    both), so its actual contribution is `add_drop_osnr + 10*log10(2)` dB
    (one-sided is better than the combined figure). An EXPRESS (interior,
    pass-through) ROADM incurs no add/drop penalty at all -- it neither adds
    nor drops the signal. The previous version charged the bare add_drop_osnr
    on EVERY propagated ROADM regardless of position, making margin
    systematically pessimistic and hop-count-dependent (verified against real
    gnpy's own get_impairment to 4 decimal places: 33.0 + 10*log10(2) =
    36.0103, gnpy's exact returned value for a one-sided terminal ROADM)."""
    # gnpy does not apply add_drop_osnr or tx_osnr to si.ase during bare element propagation;
    # they are metadata consumed only by request.py's Transceiver.update_snr(). We apply them
    # here using gnpy's own snr_sum, which normalises each penalty from 12.5 GHz to baud_rate.
    from gnpy.core.elements import Roadm as _GnpyRoadm
    from gnpy.core.utils import snr_sum as _snr_sum, lin2db as _lin2db, db2lin as _db2lin

    penalties_noise_lin = 0.0
    last_idx = len(uids_list) - 1
    for i, (_uid, _el) in enumerate(zip(uids_list, elements)):
        if isinstance(_el, _GnpyRoadm) and _uid in roadm_propagated:
            if i == 0 or i == last_idx:
                penalties_noise_lin += _db2lin(
                    -(_el.params.add_drop_osnr + _lin2db(2.0)))
            # else: express/pass-through ROADM -- no add/drop penalty.

    tx_osnr_db = float(si.tx_osnr[idx])  # at 12.5 GHz ref BW
    penalties_noise_lin += _db2lin(-tx_osnr_db)

    if penalties_noise_lin > 0.0:
        combined_penalty_db = -_lin2db(penalties_noise_lin)
        gsnr_db = float(_snr_sum(gsnr_db, baud_rate, combined_penalty_db))
        osnr_db = float(_snr_sum(osnr_db, baud_rate, combined_penalty_db))

    return gsnr_db, osnr_db


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
    cache: Optional[QoTCache] = None,
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
    # ------------------------------------------------------------------ cache
    # Content-addressed lookup, only on the model-driven path (a topo/eqpt-file
    # run is not fingerprinted by the model, so it is never cached). A key-build
    # failure degrades silently to an uncached compute — the cache never changes
    # results or error behavior.
    key = None
    if cache is not None and topo_path is None and eqpt_path is None:
        try:
            key = _cache_key(model, oms_sequence, direction, mode_id, loading,
                             center_freq_hz)
        except Exception:
            key = None
        if key is not None:
            hit = cache.get(key)
            if hit is not None:
                state, breakdown = hit
                return state, store.put(breakdown)   # fresh result_id per call

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
    # gnpy's SpectralInformation sorts carriers by frequency on construction
    # (build_si_for_loading -> create_arbitrary_spectral_information), so the
    # probe index must be resolved against a frequency-ascending channel list,
    # not the caller's original order -- allocation.py's _build_loading
    # deliberately puts the probe first, which otherwise silently reads the
    # wrong carrier's GSNR. Mirrors harvest_qot's identical sort below.
    loading_for_gnpy = LoadingState(
        channels=tuple(sorted(loading_for_gnpy.channels,
                              key=lambda c: c.center_freq_hz)))
    probe_idx = _find_probe_index(loading_for_gnpy, probe.center_freq_hz)

    pr = _propagate_loading(model, oms_sequence, direction, loading_for_gnpy, mode,
                            probe_idx, topo_path=topo_path, eqpt_path=eqpt_path)

    # ------------------------------------------------------------------ post-propagation penalties
    final_gsnr_db, final_osnr_db = _apply_penalties(
        pr.si, probe_idx, pr.uids_list, pr.elements, pr.roadm_propagated,
        pr.baud_rate, pr.final_gsnr_db, pr.final_osnr_db)

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
    for snap in pr.snapshots:
        if math.isfinite(snap.gsnr_delta_db) and snap.gsnr_delta_db < min_delta:
            min_delta = snap.gsnr_delta_db
            limiting_element_id = snap.element_id

    # ------------------------------------------------------------------ store
    breakdown = QoTBreakdown(
        snapshots=tuple(pr.snapshots),
        limiting_element_id=limiting_element_id,
    )
    result_id = store.put(breakdown)

    state = QoTState(
        gsnr_db=final_gsnr_db,
        osnr_db=final_osnr_db,
        margin_db=margin_db,
        limiting_element_id=limiting_element_id,
    )

    if key is not None:
        cache.put(key, (state, breakdown))

    return state, result_id


def _resolve_unpropagated_path(
    model: NetworkModel,
    oms_sequence: Tuple[str, ...],
    direction: Direction,
    loading_for_gnpy: LoadingState,
    mode,
):
    """Build a FRESH gnpy network from *model* and resolve *oms_sequence* to the
    ordered (uid, element) list plus the launch-primed SI, WITHOUT running the
    per-element propagation loop.

    Deliberately a standalone duplicate of ``_propagate_loading``'s setup half
    (path/ROADM/drop-ROADM resolution, SI construction, launch-transceiver feed)
    rather than a shared refactor of it: this function backs
    ``isolate_element_contribution`` (Task 14's hybrid-swap sensitivity trace),
    and ``compute_qot``/``harvest_qot`` must keep their existing behavior for
    their many other callers untouched. Every call builds brand-new gnpy element
    instances — never reuses one from a previous call — because gnpy elements
    carry in-place-mutable state (e.g. EDFA saturation/gain via power_mode) and
    ``SpectralInformation.__call__`` implementations mutate the SI object they
    are given in place (``elements.py``'s ``Fiber``/``Roadm``/``Edfa`` write
    through property setters on the SAME object, not a copy — see
    ``gnpy.core.info.SpectralInformation.apply_attenuation_db`` etc.), so an
    already-propagated element or SI reused across two different histories
    would silently desync from the trace it is meant to reproduce.
    """
    from .synthesize import build_gnpy_network, gnpy_design_network
    eqpt, network = build_gnpy_network(model)

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

    # S4-4: append the terminal drop ROADM, exactly as _propagate_loading does.
    drop_roadm = _roadm_successor(network, elements[-1]) if elements else None
    if drop_roadm is not None and drop_roadm.uid not in uids:
        uids = uids + (drop_roadm.uid,)
        elements = elements + [drop_roadm]

    si = build_si_for_loading(loading_for_gnpy, baud_rate=mode.symbol_rate_baud,
                              roll_off=mode.roll_off)

    by_uid = {n.uid: n for n in network.nodes}
    uids_list = list(uids)
    launch_trx = _find_launch_transceiver(network, uids_list, by_uid)
    if launch_trx is not None:
        si = launch_trx(si)

    return network, uids_list, elements, si


def isolate_element_contribution(
    *,
    model_a: NetworkModel,
    model_b: NetworkModel,
    oms_sequence: Tuple[str, ...],
    direction: Direction,
    mode_id: str,
    loading: LoadingState,
    position: int,
    center_freq_hz: Optional[float] = None,
) -> ElementSnapshot:
    """Isolate one element's OWN marginal GSNR contribution, free of downstream
    leakage, via a hybrid-propagation swap (Task 14).

    ``whatif_sensitivity`` diffs two branches' full propagations element by
    element. Even though each element's stored ``gsnr_delta_db`` is already a
    marginal (hop-to-hop) figure, it still depends on the *input* SI arriving
    at that element -- which is the product of everything propagated upstream.
    So a perturbation at one element echoes as a smaller, spurious delta at
    every element downstream of it, even when those elements' own physical
    parameters are identical in both branches (confirmed empirically: a +4 dB
    NF bump on the first amp of a 10-span OMS shows -1.53 dB at that amp, but
    also +0.29 dB / +0.19 dB / ... at untouched downstream amps).

    This function answers a narrower question: "if ONLY this element's own
    parameters had model_b's values, holding every upstream element's own
    contribution fixed at model_a's trace, what would THIS element's own
    marginal gsnr_delta_db be?" It propagates *model_a*'s own elements for
    every position strictly before *position*, then substitutes *model_b*'s
    version of the element AT *position* for the final step, and returns the
    resulting snapshot. Comparing this isolated value to model_a's own
    baseline delta at *position* (as ``whatif_sensitivity`` does) yields ~0 for
    an untouched element (model_b's version has identical physics, so the
    hybrid trace is indistinguishable from model_a's own) regardless of what
    changed upstream, and the element's true own-contribution shift when
    *position* is the perturbed element itself.

    Requires model_a and model_b to resolve to the identical physical path
    (element uids) -- true for any pair of branches related by
    inject_degradation, which perturbs impairment parameters only, never
    topology. Raises ValueError if the resolved paths differ.
    """
    mode = model_a.modes.get(mode_id)  # raises KeyError if unknown; shared by both models

    # ---- probe selection: mirrors compute_qot's probe-selection block ----
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

    loading_for_gnpy = _ensure_min_two_channels(loading, probe.center_freq_hz)
    loading_for_gnpy = LoadingState(
        channels=tuple(sorted(loading_for_gnpy.channels,
                              key=lambda c: c.center_freq_hz)))
    probe_idx = _find_probe_index(loading_for_gnpy, probe.center_freq_hz)

    net_a, uids_a, elements_a, si = _resolve_unpropagated_path(
        model_a, oms_sequence, direction, loading_for_gnpy, mode)
    net_b, uids_b, elements_b, _si_b = _resolve_unpropagated_path(
        model_b, oms_sequence, direction, loading_for_gnpy, mode)

    if uids_a != uids_b:
        raise ValueError(
            "isolate_element_contribution requires model_a and model_b to "
            "resolve the identical physical path (same OMS/element uids); got "
            f"{uids_a!r} vs {uids_b!r}. inject_degradation only perturbs "
            "impairment parameters, never topology -- a mismatch here means "
            "the two models are not comparable branches of the same network."
        )
    if not (0 <= position < len(uids_a)):
        raise IndexError(
            f"position {position} out of range for path of length {len(uids_a)}"
        )

    from gnpy.core.elements import Roadm as _GnpyRoadm

    prev_gsnr_db = math.inf
    snapshot: Optional[ElementSnapshot] = None
    for i in range(0, position + 1):
        if i == position:
            uid, el, net = uids_b[i], elements_b[i], net_b
        else:
            uid, el, net = uids_a[i], elements_a[i], net_a

        if isinstance(el, _GnpyRoadm):
            from_uid = (
                uids_a[i - 1] if i > 0
                else _trx_neighbor_uid(net, el, predecessor=True)
            )
            to_uid = (
                uids_a[i + 1] if i < len(uids_a) - 1
                else _trx_neighbor_uid(net, el, predecessor=False)
            )
            if (from_uid is not None and to_uid is not None
                    and any(rp.from_degree == from_uid and rp.to_degree == to_uid
                            for rp in el.roadm_paths)):
                si = el(si, degree=to_uid, from_degree=from_uid)
        else:
            si = el(si)

        gsnr_db, osnr_db = _extract_gsnr_osnr(si, probe_idx)
        ase_w = float(si.ase[probe_idx])
        nli_w = float(si.nli[probe_idx])
        ase_contrib_db = 10.0 * math.log10(ase_w) if ase_w > 0.0 else -math.inf
        nli_contrib_db = 10.0 * math.log10(nli_w) if nli_w > 0.0 else -math.inf
        gsnr_delta_db = gsnr_db - prev_gsnr_db

        if i == position:
            snapshot = ElementSnapshot(
                element_id=uid,
                gsnr_db_after=gsnr_db,
                osnr_db_after=osnr_db,
                gsnr_delta_db=gsnr_delta_db,
                ase_contribution_db=ase_contrib_db,
                nli_contribution_db=nli_contrib_db,
            )

        if math.isfinite(gsnr_db):
            prev_gsnr_db = gsnr_db

    assert snapshot is not None  # loop always runs position+1 >= 1 iterations
    return snapshot


def harvest_cache_key(
    model: NetworkModel, oms_sequence: Tuple[str, ...], direction: Direction,
    mode_id: str,
) -> tuple:
    """Content-addressed key for a full-comb harvest: path + direction + mode +
    physical fingerprint — deliberately no probe frequency, because one harvest
    answers every slot at once (unlike ``_cache_key``, which is one probe)."""
    return (
        tuple(oms_sequence),
        direction.value,
        mode_id,
        _path_physical_fingerprint(model, oms_sequence, direction),
    )


def harvest_qot(
    model: NetworkModel,
    oms_sequence: Tuple[str, ...],
    direction: Direction,
    mode_id: str,
    full_comb: LoadingState,
) -> dict[int, QoTState]:
    """Propagate a full-grid *full_comb* loading once and harvest every carrier's
    GSNR/OSNR, keyed by grid slot — the mechanism that makes ``FillPolicy.FULL``
    cheap: one propagation instead of one per candidate probe frequency.

    No store writes (no per-slot breakdown persisted) and no per-element
    snapshots kept; callers that need the full breakdown for one slot should use
    ``compute_qot`` for that slot instead.

    The returned dict may be missing entries for slots an amplifier or ROADM's
    passband edge filters demux out (e.g. under this repo's grid/amp-band
    config, the topmost grid slot is always dropped) — callers must check
    membership (``slot in vec``) rather than assume every requested slot comes
    back.
    """
    from ..model.spectrum import SpectrumGrid

    grid = SpectrumGrid.default()
    mode = model.modes.get(mode_id)  # raises KeyError if unknown

    # Frequency-sort so the SI carrier index aligns with gnpy's ascending-
    # frequency SI arrays (mirrors the convention in _per_path_loading).
    sorted_channels = tuple(sorted(full_comb.channels, key=lambda c: c.center_freq_hz))
    loading_sorted = LoadingState(channels=sorted_channels)

    pr = _propagate_loading(model, oms_sequence, direction, loading_sorted, mode,
                            probe_idx=0)  # probe_idx only feeds discarded snapshots

    # Read carrier positions back from the *propagated* SI rather than trusting
    # positional alignment with sorted_channels: a carrier can be dropped along
    # the path (e.g. an amp/ROADM band-edge filter demuxes out a channel whose
    # slot edge falls outside its passband), which shrinks and reindexes the SI.
    # Matching by the SI's own post-propagation frequency is correct regardless
    # of whether/where a channel was dropped.
    out: dict[int, QoTState] = {}
    for i, freq_hz in enumerate(pr.si.frequency):
        gsnr_db, osnr_db = _extract_gsnr_osnr(pr.si, i)
        gsnr_db, osnr_db = _apply_penalties(
            pr.si, i, pr.uids_list, pr.elements, pr.roadm_propagated,
            pr.baud_rate, gsnr_db, osnr_db)
        slot = grid.slot_of(float(freq_hz))
        out[slot] = QoTState(
            gsnr_db=gsnr_db,
            osnr_db=osnr_db,
            margin_db=gsnr_db - mode.required_gsnr_db,
            limiting_element_id=None,
        )
    return out


def gated_qot(
    *,
    model: NetworkModel,
    store: QoTResultStore,
    oms_sequence: Tuple[str, ...],
    mode_id: str,
    loading: LoadingState,
    center_freq_hz: Optional[float] = None,
    cache: Optional[QoTCache] = None,
) -> Tuple[QoTState, str]:
    """Return the worse of forward/backward QoT, along with its breakdown id."""
    fwd_state, fwd_rid = compute_qot(model=model, store=store,
                                     oms_sequence=oms_sequence,
                                     direction=Direction.FORWARD,
                                     mode_id=mode_id, loading=loading,
                                     center_freq_hz=center_freq_hz, cache=cache)
    bwd_state, bwd_rid = compute_qot(model=model, store=store,
                                     oms_sequence=oms_sequence,
                                     direction=Direction.BACKWARD,
                                     mode_id=mode_id, loading=loading,
                                     center_freq_hz=center_freq_hz, cache=cache)
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


def unattributed_channel_freqs_hz(model: NetworkModel, loading: LoadingState) -> Tuple[float, ...]:
    """Frequencies in *loading* not explained by any committed lightpath.

    These are the channels recompute_qot_under_loading broadcasts as an
    interferer to EVERY lightpath in the model (no OMS to scope them to —
    see that function's docstring). Exposed so a caller can see when this
    happened, since it's not visible from the per-lightpath results alone.

    Read-only, side-effect-free duplicate of the same lit/known/uncommitted
    computation recompute_qot_under_loading performs internally — kept
    byte-identical in logic so the two never silently diverge.
    """
    from ..model.spectrum import SpectrumGrid, build_spectrum_state

    grid = SpectrumGrid.default()
    model_state = build_spectrum_state(model, grid)
    known = 0
    for bits in model_state.values():
        known |= bits
    lit = 0
    for ch in loading.channels:
        try:
            lit |= 1 << grid.slot_of(ch.center_freq_hz)
        except ValueError:
            continue
    uncommitted = lit & ~known & grid.all_slots_mask

    freqs = []
    bits = uncommitted
    while bits:
        low = bits & -bits
        slot = low.bit_length() - 1
        freqs.append(grid.freq(slot))
        bits ^= low
    return tuple(freqs)


def recompute_qot_under_loading(
    *, model: NetworkModel, store: QoTResultStore, loading: LoadingState,
    cache: Optional[QoTCache] = None,
) -> dict[str, Tuple[QoTState, str]]:
    """Compute gated QoT for every lightpath in model under *loading*.

    *loading* is honored verbatim (CLAUDE.md's adapter contract: a loading state
    is a first-class input, not "the current network") — including channels that
    are not, and never were, provisioned. That is what makes a make-before-break
    overlap (old ∪ new, both lit on a shared span, *before* either is committed)
    evaluable without provisioning the new channel first.

    Each lightpath's interferer comb is built from *its own* OMS occupancy (the
    per-OMS spectrum bitmask) for the portion of *loading* that traces back to a
    channel already committed elsewhere in the model — that restriction is what
    keeps a committed channel on a physically disjoint fiber from being counted
    as an interferer (NLI over-count) or duplicated as a same-frequency carrier
    (malformed NLI) via ordinary cross-fiber wavelength reuse — S4-6/S8-5,
    unchanged by this fix. A slot in *loading* that is NOT explained by any
    committed lightpath anywhere in the model — i.e. not corroborated by
    anything currently committed — has no known OMS to restrict it to (a bare
    Channel carries a center frequency, not a path), so it is added to *every*
    lightpath's interferer comb: the caller constructed this loading set
    deliberately, and CLAUDE.md's contract is to trust it, not to silently drop
    the part the model can't corroborate. (Internal callers that need a new
    channel scoped to one specific OMS provision it onto a clone first, so it
    becomes a committed, properly-scoped channel — see loading_from_model.) Use
    `unattributed_channel_freqs_hz` to see which frequencies (if any) triggered
    this broadcast for a given call.

    KNOWN LIMITATION — same-frequency reroute / make-before-break is NOT
    resolved by this function when the new channel's frequency coincides with
    a channel still committed elsewhere in the model. Concretely: an old
    lightpath occupies frequency F on OMS-A; the caller's *loading* includes F
    meaning "a new, not-yet-provisioned channel at F on OMS-B" (a different
    fiber). Because F is still in `known` (the old lightpath on OMS-A hasn't
    been torn down), that slot is classified as "committed, scope to its real
    OMS" (OMS-A) rather than "uncommitted, broadcast everywhere" — so OMS-B's
    lightpaths never see it at all. This is silent: no error, no signal, just
    a QoT result that doesn't include the intended new channel. It cannot be
    resolved in general without adding OMS/path information to
    `Channel`/`LoadingState` (out of scope here) — a bare frequency cannot
    distinguish "legitimately reused where it already is" from "meant to move
    somewhere new that happens to share a frequency". For that specific case
    today, callers should use `compute_qot` on the new lightpath's own intended
    path instead, which (per the exact per-path channel comb fix) validates and
    honors an exact per-path channel comb with no such ambiguity.

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

    # Slots explained by SOME committed lightpath, on any OMS. Used below to
    # distinguish "this lit slot is a committed channel, scope it to its real
    # OMS" from "this lit slot has no committed source, honor it everywhere".
    known = 0
    for bits in model_state.values():
        known |= bits

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
        # Committed channels on lp's own OMS (unchanged, fiber-scoped) OR'd with
        # lit-but-uncommitted slots (additive, unscoped — see docstring).
        own_committed = occupied_along(model_state, lp.oms_sequence) & lit
        uncommitted = lit & ~known & grid.all_slots_mask
        occ = own_committed | uncommitted
        per_path = _per_path_loading(grid, occ, probe_slot, lp.mode_id)
        state, rid = gated_qot(model=model, store=store,
                               oms_sequence=lp.oms_sequence,
                               mode_id=lp.mode_id, loading=per_path,
                               center_freq_hz=grid.freq(probe_slot), cache=cache)
        model.set_qot_state(lp.id, state)
        results[lp.id] = (state, rid)
    return results
