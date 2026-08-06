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
    s1 = store.create(); s2 = store.create(); store.create()
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


def test_active_branch_survives_cap_eviction():
    """A long exploratory session (many create()/branch() calls elsewhere on
    the same store) must not evict the branch you are actively working on --
    it should be exempt from cap eviction until you branch/restore away from
    it, even though it's the oldest entry by insertion order."""
    store = SnapshotStore(initial=_empty(), max_snapshots=3)
    root = store.create()
    bid = store.branch(root)          # bid is now the active branch
    for _ in range(10):               # far more than max_snapshots elsewhere
        store.create()
    assert store.get(bid) is not None
    store.restore(bid)                # must not raise KeyError


def test_active_branch_survives_ttl_reap():
    store = SnapshotStore(initial=_empty(), ttl_seconds=0.05)
    root = store.create()
    bid = store.branch(root)
    time.sleep(0.1)
    store.reap()
    assert store.get(bid) is not None


def test_branching_away_unprotects_previous_branch():
    """Once you branch/restore to a new id, the OLD current id loses its
    eviction immunity -- protection tracks the single active branch, not
    every branch ever created."""
    store = SnapshotStore(initial=_empty(), max_snapshots=3)
    root = store.create()
    old_bid = store.branch(root)
    store.branch(old_bid)              # moves current off old_bid
    for _ in range(10):
        store.create()
    with pytest.raises(KeyError):
        store.get(old_bid)
