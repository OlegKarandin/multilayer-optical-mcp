from __future__ import annotations
import time
import uuid
from collections import OrderedDict
from typing import Dict, Tuple
from .network import NetworkModel


class SnapshotStore:
    def __init__(
        self,
        initial: NetworkModel,
        max_snapshots: int | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        self._current = initial
        self._snapshots: OrderedDict[str, NetworkModel] = OrderedDict()
        self._created_at: Dict[str, float] = {}
        self._max = max_snapshots
        self._ttl = ttl_seconds

    def _store(self, sid: str, model: NetworkModel) -> None:
        self._snapshots[sid] = model
        self._created_at[sid] = time.monotonic()
        if self._max is not None and len(self._snapshots) > self._max:
            oldest, _ = self._snapshots.popitem(last=False)
            self._created_at.pop(oldest, None)

    def current(self) -> NetworkModel:
        return self._current

    def create(self) -> str:
        sid = uuid.uuid4().hex
        self._store(sid, self._current.clone())
        return sid

    def branch(self, parent_id: str) -> str:
        parent = self._snapshots[parent_id]
        bid = uuid.uuid4().hex
        new = parent.clone()  # always unfrozen working copy
        self._store(bid, new)
        self._current = new
        return bid

    def get(self, sid: str) -> NetworkModel:
        """Return a FROZEN clone of the stored snapshot. Callers can read it but
        cannot mutate it — a stored snapshot can never be corrupted through
        get(). Mutate via branch()/current() or the returned model's clone()."""
        return self._snapshots[sid].clone().freeze()

    def restore(self, sid: str) -> None:
        self._current = self._snapshots[sid].clone()

    def reap(self) -> Tuple[str, ...]:
        if self._ttl is None:
            return ()
        now = time.monotonic()
        expired = [sid for sid, t in self._created_at.items() if now - t > self._ttl]
        for sid in expired:
            self._snapshots.pop(sid, None)
            self._created_at.pop(sid, None)
        return tuple(expired)

    def put(self, model: NetworkModel) -> str:
        """Register an externally-constructed model under a fresh id (stores a
        clone, so later mutation of the argument cannot corrupt the snapshot).
        Phase 7 uses this to record a commit's intended end-state for reconcile."""
        sid = uuid.uuid4().hex
        self._store(sid, model.clone())
        return sid

    def diff(self, a_id: str, b_id: str) -> dict:
        return diff_models(self._snapshots[a_id], self._snapshots[b_id])


def diff_models(a: NetworkModel, b: NetworkModel) -> dict:
    """Structured per-registry delta between two model objects. Snapshot-agnostic
    (a free function, not a store method) so reconcile can diff live-vs-intended
    model objects without first registering both as snapshots."""
    return {
        "fiber_types": _delta(a._fiber_types, b._fiber_types),
        "fibers": _delta(a._fibers, b._fibers),
        "amplifiers": _delta(a._amplifiers, b._amplifiers),
        "oms": _delta(a._oms, b._oms),
        "lightpaths": _delta(a._lightpaths, b._lightpaths),
        "ip_links": _delta(a._ip_links, b._ip_links),
        "routers": _delta(a._routers, b._routers),
        "services": _delta(a._services, b._services),
        "srlgs": _delta(a._srlgs, b._srlgs),
        "risk_groups": _delta(a._risk_groups, b._risk_groups),
        "qot_state": _delta(a._qot_state, b._qot_state),
        "failed_assets": _delta_set(a._failed_assets, b._failed_assets),
    }


def _delta(a: dict, b: dict) -> dict:
    a_keys, b_keys = set(a), set(b)
    return {
        "added": tuple(sorted(b_keys - a_keys)),
        "removed": tuple(sorted(a_keys - b_keys)),
        "modified": tuple(sorted(k for k in a_keys & b_keys if a[k] != b[k])),
    }


def _delta_set(a: set, b: set) -> dict:
    return {
        "added": tuple(sorted(b - a)),
        "removed": tuple(sorted(a - b)),
        "modified": (),
    }
