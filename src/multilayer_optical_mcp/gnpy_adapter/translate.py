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
    from gnpy.tools.json_io import load_equipment, load_network

    eqpt = load_equipment(eqpt_path)
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


def build_si_for_loading(
    loading: LoadingState,
    *,
    baud_rate: float,
    roll_off: float,
    tx_osnr: float,
    tx_power_dbm: float = -20.0,
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
        Default per-channel launch power in dBm when the channel's own
        power_dbm field is 0.0.  Defaults to -20 dBm (1e-5 W).
    """
    from gnpy.core.info import create_arbitrary_spectral_information

    if not loading.channels:
        # Return an empty SI by constructing with zero-length arrays.
        import numpy as np

        empty = np.array([], dtype=float)
        return create_arbitrary_spectral_information(
            frequency=empty,
            signal=empty,
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
    # Use channel's own power if non-zero, otherwise fall back to tx_power_dbm.
    powers_w = np.array(
        [
            _dbm_to_watt(ch.power_dbm if ch.power_dbm != 0.0 else tx_power_dbm)
            for ch in loading.channels
        ],
        dtype=float,
    )
    tx_power_w = np.full(len(loading.channels), _dbm_to_watt(tx_power_dbm), dtype=float)
    baud_rates = np.full(len(loading.channels), baud_rate, dtype=float)
    roll_offs = np.full(len(loading.channels), roll_off, dtype=float)
    tx_osnrs = np.full(len(loading.channels), tx_osnr, dtype=float)
    slot_widths = np.array([ch.slot_width_hz for ch in loading.channels], dtype=float)
    delta_pdb = np.zeros(len(loading.channels), dtype=float)
    labels = np.array([ch.mode_id for ch in loading.channels])

    return create_arbitrary_spectral_information(
        frequency=frequencies,
        signal=powers_w,
        baud_rate=baud_rates,
        tx_osnr=tx_osnrs,
        tx_power=tx_power_w,
        roll_off=roll_offs,
        slot_width=slot_widths,
        delta_pdb_per_channel=delta_pdb,
        label=labels,
    )
