import math

import pytest

from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot, harvest_qot
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import Direction
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.spectrum import SpectrumGrid
from tests.gnpy_adapter.test_compute_qot import _toy_model

MODE = "400G@7.1dB"
GRID = SpectrumGrid.default()


def _full_comb(probe_slot):
    # Every slot is lit regardless of probe_slot (the resulting channel *set* is
    # identical for any probe_slot; compute_qot selects the probe by matching
    # center_freq_hz, not position). Channels must come out frequency-ascending:
    # gnpy's SpectralInformation.__init__ unconditionally argsorts by frequency,
    # so compute_qot's positional probe_idx (found in the *pre-sort* loading)
    # only lines up with the post-sort SI arrays when the input was already
    # ascending -- the same convention _per_path_loading documents/relies on.
    return LoadingState(tuple(
        Channel(GRID.freq(s), GRID.spacing_hz, None, MODE)
        for s in range(GRID.num_slots)
    ))


@pytest.mark.parametrize("direction", [Direction.FORWARD, Direction.BACKWARD])
def test_harvest_matches_per_slot_compute_qot(direction):
    n = _toy_model()
    oms = ("oms-AZ",) if direction == Direction.FORWARD else ("oms-AZ",)
    vec = harvest_qot(n, oms, direction, MODE, _full_comb(20))
    for slot in (10, 20, 30):
        state, _ = compute_qot(
            model=n, store=QoTResultStore(), oms_sequence=oms,
            direction=direction, mode_id=MODE, loading=_full_comb(slot),
            center_freq_hz=GRID.freq(slot))
        assert math.isclose(vec[slot].gsnr_db, state.gsnr_db, rel_tol=1e-9, abs_tol=1e-9)
        assert math.isclose(vec[slot].osnr_db, state.osnr_db, rel_tol=1e-9, abs_tol=1e-9)
