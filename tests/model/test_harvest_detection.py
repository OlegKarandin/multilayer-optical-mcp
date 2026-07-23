from unittest.mock import patch

import copy

from multilayer_optical_mcp.model.allocation import make_adapter_evaluator
from multilayer_optical_mcp.model.qot_results import QoTResultStore, HarvestCache
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import Direction
from multilayer_optical_mcp.model.spectrum import SpectrumGrid
from tests.gnpy_adapter.test_compute_qot import _toy_model

MODE = "400G@7.1dB"
GRID = SpectrumGrid.default()


def _full_comb(probe_slot):
    probe = Channel(GRID.freq(probe_slot), GRID.spacing_hz, None, MODE)
    others = tuple(Channel(GRID.freq(s), GRID.spacing_hz, None, MODE)
                   for s in range(GRID.num_slots) if s != probe_slot)
    return LoadingState((probe,) + others)


def test_full_grid_harvests_once_across_probe_slots():
    n = _toy_model()
    hc = HarvestCache()
    ev = make_adapter_evaluator(n, QoTResultStore(), harvest_cache=hc)
    import multilayer_optical_mcp.model.allocation as alloc
    with patch.object(alloc, "harvest_qot", wraps=alloc.harvest_qot) as spy:
        for slot in (10, 20, 30):
            ev(oms_sequence=("oms-AZ",), direction=Direction.FORWARD,
               mode_id=MODE, loading=_full_comb(slot))
        assert spy.call_count == 1          # one propagation serves every probe slot


def test_subset_loading_does_not_harvest():
    n = _toy_model()
    hc = HarvestCache()
    ev = make_adapter_evaluator(n, QoTResultStore(), harvest_cache=hc)
    subset = LoadingState((Channel(GRID.freq(20), GRID.spacing_hz, None, MODE),))
    import multilayer_optical_mcp.model.allocation as alloc
    with patch.object(alloc, "harvest_qot", wraps=alloc.harvest_qot) as spy:
        ev(oms_sequence=("oms-AZ",), direction=Direction.FORWARD,
           mode_id=MODE, loading=subset)
        assert spy.call_count == 0


def test_harvest_key_misses_when_fiber_loss_changes():
    n = _toy_model()
    hc = HarvestCache()
    ev = make_adapter_evaluator(n, QoTResultStore(), harvest_cache=hc)
    ev(oms_sequence=("oms-AZ",), direction=Direction.FORWARD,
       mode_id=MODE, loading=_full_comb(20))
    # mutate a GSNR-relevant physical input -> fingerprint must flip -> new propagation
    n2 = _toy_model()
    ft = n2.get_fiber_type("SSMF")
    n2.register_fiber_type(type(ft)(type_variety="SSMF", loss_coef_db_per_km=0.25))
    ev2 = make_adapter_evaluator(n2, QoTResultStore(), harvest_cache=hc)
    import multilayer_optical_mcp.model.allocation as alloc
    from unittest.mock import patch
    with patch.object(alloc, "harvest_qot", wraps=alloc.harvest_qot) as spy:
        ev2(oms_sequence=("oms-AZ",), direction=Direction.FORWARD,
            mode_id=MODE, loading=_full_comb(20))
        assert spy.call_count == 1          # different fiber loss -> cache miss
