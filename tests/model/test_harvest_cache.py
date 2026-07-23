from multilayer_optical_mcp.model.qot_results import HarvestCache
from multilayer_optical_mcp.model.qot import QoTState


def _vec(g):
    return {0: QoTState(gsnr_db=g, osnr_db=30.0, margin_db=g - 7.1, limiting_element_id=None)}


def test_harvest_cache_get_put_roundtrip():
    c = HarvestCache()
    key = ("oms-AZ", "forward", "400G@7.1dB", ("fp",))
    assert c.get(key) is None
    c.put(key, _vec(17.8))
    assert c.get(key)[0].gsnr_db == 17.8


def test_harvest_cache_evicts_oldest():
    c = HarvestCache(maxsize=2)
    for i in range(3):
        c.put((i,), _vec(float(i)))
    assert c.get((0,)) is None      # evicted
    assert c.get((2,)) is not None
