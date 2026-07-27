from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Tuple

from ..model.network import NetworkModel
from .loading import LoadingState

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EQPT = REPO_ROOT / "eqpt" / "eqpt_config.json"
DEFAULT_TOPO = REPO_ROOT / "topologies" / "toy_2span.json"


def load_toy(
    eqpt_path: Path = DEFAULT_EQPT,
    topo_path: Path = DEFAULT_TOPO,
) -> tuple[Any, Any]:
    from gnpy.tools.json_io import load_equipment, load_network, load_json

    # gnpy 2.14+ requires extra_configs passed explicitly: scan the equipment
    # directory for JSON files (filename key → parsed dict), so that
    # advanced_config_from_json = "file.json" resolves correctly.
    eqpt_dir = Path(eqpt_path).parent
    extra_configs = {
        p.name: load_json(p)
        for p in eqpt_dir.iterdir()
        if p.suffix.lower() == ".json" and p != Path(eqpt_path)
    }
    eqpt = load_equipment(eqpt_path, extra_configs)
    network = load_network(topo_path, eqpt)
    return eqpt, network


def resolve_oms_path_to_uids(
    model: NetworkModel,
    oms_sequence: Tuple[str, ...],
) -> Tuple[str, ...]:
    """Expand an ordered sequence of OMS ids into the flat uid list of their elements.

    Raises KeyError if any OMS id is not registered in *model*.
    """
    uids: list[str] = []
    for oms_id in oms_sequence:
        oms = model.get_oms(oms_id)  # KeyError propagates naturally
        uids.extend(oms.elements)
    return tuple(uids)


def reverse_oms_sequence(
    model: NetworkModel,
    oms_sequence: Tuple[str, ...],
) -> "Tuple[str, ...] | None":
    """Resolve the physically separate reverse path for *oms_sequence*.

    For each OMS the importer builds a paired reverse OMS with swapped endpoints
    (``oms_<src>_<dst>`` ↔ ``oms_<dst>_<src>``), each with its own amplifier
    chain. The reverse of a forward path is the reversed list of those paired
    OMS, so the returned sequence traverses the destination-to-source amp chains
    in natural (travel) order.

    Returns ``None`` when any leg has no paired reverse OMS, so the caller can
    raise a clear error rather than silently reversing the forward element list
    (which would walk the forward fiber's amps and discard the reverse chain's
    per-direction impairments — S4-2/S4-3).

    Raises ``ValueError`` when a leg's reverse (dst, src) node pair matches
    MORE THAN ONE OMS (parallel/diverse routes between the same two sites) --
    the data model has no per-OMS disambiguator beyond node pair, so guessing
    would silently pick an arbitrary one of the candidates and propagate
    through the wrong route's amplifier chain. A loud failure here is the safe
    behavior; there is currently no way to correctly resolve this case.
    """
    by_pair: dict[tuple[str, str], list[str]] = {}
    for o in model.list_oms():
        by_pair.setdefault((o.src_node_id, o.dst_node_id), []).append(o.id)
    reverse: list[str] = []
    for oms_id in reversed(oms_sequence):
        oms = model.get_oms(oms_id)
        candidates = by_pair.get((oms.dst_node_id, oms.src_node_id))
        if not candidates:
            return None
        if len(candidates) > 1:
            raise ValueError(
                f"ambiguous reverse OMS for {oms_id!r}: {len(candidates)} OMS "
                f"share the node pair ({oms.dst_node_id!r}, {oms.src_node_id!r}): "
                f"{sorted(candidates)!r} -- parallel routes between the same "
                f"endpoints cannot be disambiguated by node pair alone"
            )
        reverse.append(candidates[0])
    return tuple(reverse)


def build_si_for_loading(
    loading: LoadingState,
    *,
    baud_rate: float,
    roll_off: float,
    tx_osnr: float = 35.0,
    tx_power_dbm: float = -20.0,
    tx_launch_power_dbm: float = 0.0,
) -> Any:
    """Build a gnpy SpectralInformation with one carrier per channel in *loading*.

    Uses ``create_arbitrary_spectral_information`` so the carrier set matches
    *exactly* the channels in the LoadingState rather than a filled grid.

    Parameters
    ----------
    loading:
        The set of channels to model.
    baud_rate:
        Symbol rate in Baud (Hz).
    roll_off:
        Nyquist roll-off factor (dimensionless, e.g. 0.15).
    tx_osnr:
        Transmitter OSNR in dB.
    tx_power_dbm:
        Default per-channel *fiber-input* power (pch) in dBm used when the
        channel's own ``power_dbm`` is ``None`` (S2-2). This is the ROADM
        ``target_pch_out`` the channel enters the line at. Defaults to -20 dBm
        (1e-5 W). A channel carrying a literal 0.0 now means 0 dBm, not "default".
    tx_launch_power_dbm:
        The *transponder launch* power in dBm, distinct from pch. It sets gnpy's
        ``tx_power`` and hence the TX-OSNR noise floor
        (``noise_tx = tx_power / tx_osnr_linear``). Defaults to 0 dBm (1e-3 W),
        the standard coherent-pluggable reference. Building this from the -20 dBm
        pch default would make the TX-OSNR budget 20 dB too optimistic (S2-3).
    """
    from gnpy.core.info import create_arbitrary_spectral_information

    if not loading.channels:
        # Return an empty SI by constructing with zero-length arrays.
        import numpy as np

        empty = np.array([], dtype=float)
        return create_arbitrary_spectral_information(
            frequency=empty,
            pch=empty,
            baud_rate=empty,
            tx_osnr=empty,
            tx_power=empty,
            roll_off=empty,
            slot_width=empty,
            delta_pdb_per_channel=empty,
            label=empty,
        )

    def _dbm_to_watt(dbm: float) -> float:
        return 10.0 ** (dbm / 10.0) * 1e-3

    import numpy as np

    frequencies = np.array([ch.center_freq_hz for ch in loading.channels], dtype=float)
    # S2-2: use the channel's own power when set, else fall back to tx_power_dbm.
    # None is the "default" sentinel; 0.0 is now a literal 0 dBm launch.
    powers_w = np.array(
        [
            _dbm_to_watt(ch.power_dbm if ch.power_dbm is not None else tx_power_dbm)
            for ch in loading.channels
        ],
        dtype=float,
    )
    # TX launch power (transponder), distinct from pch — sets the TX-OSNR floor.
    tx_power_w = np.full(len(loading.channels), _dbm_to_watt(tx_launch_power_dbm),
                         dtype=float)
    # S2-4: prefer each channel's own baud/roll-off; fall back to the scalar
    # defaults when unset, so mixed-baud loading states get per-carrier shapes.
    baud_rates = np.array(
        [ch.baud_rate_hz if ch.baud_rate_hz is not None else baud_rate
         for ch in loading.channels],
        dtype=float,
    )
    roll_offs = np.array(
        [ch.roll_off if ch.roll_off is not None else roll_off
         for ch in loading.channels],
        dtype=float,
    )
    tx_osnrs = np.full(len(loading.channels), tx_osnr, dtype=float)
    slot_widths = np.array([ch.slot_width_hz for ch in loading.channels], dtype=float)
    delta_pdb = np.zeros(len(loading.channels), dtype=float)
    labels = np.array([ch.mode_id for ch in loading.channels])

    return create_arbitrary_spectral_information(
        frequency=frequencies,
        pch=powers_w,
        baud_rate=baud_rates,
        tx_osnr=tx_osnrs,
        tx_power=tx_power_w,
        roll_off=roll_offs,
        slot_width=slot_widths,
        delta_pdb_per_channel=delta_pdb,
        label=labels,
    )
