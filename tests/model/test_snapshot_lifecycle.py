import time
import pytest
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.snapshots import SnapshotStore


def _empty():
    return NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))


def test_max_snapshots_cap_evicts_oldest():
    store = SnapshotStore(initial=_empty(), max_snapshots=3)
    s1 = store.create(); s2 = store.create(); s3 = store.create()
    s4 = store.create()  # evicts s1
    with pytest.raises(KeyError):
        store.get(s1)
    assert store.get(s2) is not None
    assert store.get(s4) is not None


def test_ttl_reaps_expired():
    store = SnapshotStore(initial=_empty(), ttl_seconds=0.05)
    s = store.create()
    time.sleep(0.1)
    store.reap()
    with pytest.raises(KeyError):
        store.get(s)


def test_default_no_cap_no_ttl():
    store = SnapshotStore(initial=_empty())
    ids = [store.create() for _ in range(10)]
    for sid in ids:
        assert store.get(sid) is not None
