import time
import pytest
from multilayer_optical_mcp.model.qot import (
    QoTBreakdown, ElementSnapshot,
)
from multilayer_optical_mcp.model.qot_results import QoTResultStore


def _bd():
    return QoTBreakdown(
        snapshots=(
            ElementSnapshot("amp1", 22.0, 23.0, 0.0, 0.0, 0.0),
            ElementSnapshot("fiber1", 19.0, 20.5, -3.0, -1.5, -1.5),
        ),
        limiting_element_id="fiber1",
    )


def test_put_returns_unique_id_and_get_round_trips():
    store = QoTResultStore()
    rid1 = store.put(_bd())
    rid2 = store.put(_bd())
    assert rid1 != rid2
    assert store.get(rid1).limiting_element_id == "fiber1"


def test_get_unknown_id_raises():
    store = QoTResultStore()
    with pytest.raises(KeyError):
        store.get("nope")


def test_cap_evicts_oldest():
    store = QoTResultStore(max_results=2)
    r1 = store.put(_bd()); r2 = store.put(_bd()); r3 = store.put(_bd())
    with pytest.raises(KeyError):
        store.get(r1)
    assert store.get(r2) is not None
    assert store.get(r3) is not None


def test_ttl_reaps_expired():
    store = QoTResultStore(ttl_seconds=0.05)
    rid = store.put(_bd())
    time.sleep(0.1)
    store.reap()
    with pytest.raises(KeyError):
        store.get(rid)


def test_put_reaps_expired_results(monkeypatch):
    """put() must call reap() lazily so an expired entry is gone by the next
    write -- not just reachable via an explicit reap() call (already covered
    by test_ttl_reaps_expired above)."""
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    store = QoTResultStore(ttl_seconds=10.0)
    old = store.put(_bd())

    now[0] += 20.0          # advance past the TTL
    store.put(_bd())        # a later write should trigger reap()

    with pytest.raises(KeyError):
        store.get(old)
