from __future__ import annotations
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple
from .qot import QoTBreakdown, QoTState


class QoTResultStore:
    def __init__(
        self,
        max_results: Optional[int] = 512,
        ttl_seconds: Optional[float] = 600.0,
    ) -> None:
        self._items: OrderedDict[str, QoTBreakdown] = OrderedDict()
        self._created_at: Dict[str, float] = {}
        self._max = max_results
        self._ttl = ttl_seconds

    def put(self, breakdown: QoTBreakdown) -> str:
        rid = uuid.uuid4().hex
        self._items[rid] = breakdown
        self._created_at[rid] = time.monotonic()
        if self._max is not None and len(self._items) > self._max:
            oldest, _ = self._items.popitem(last=False)
            self._created_at.pop(oldest, None)
        return rid

    def get(self, rid: str) -> QoTBreakdown:
        return self._items[rid]

    def reap(self) -> Tuple[str, ...]:
        if self._ttl is None:
            return ()
        now = time.monotonic()
        expired = [r for r, t in self._created_at.items() if now - t > self._ttl]
        for r in expired:
            self._items.pop(r, None)
            self._created_at.pop(r, None)
        return tuple(expired)


class QoTCache:
    """Content-addressed memo of ``compute_qot`` results, keyed by a fingerprint
    of every GSNR input (see ``adapter._cache_key``). Off-model and injected like
    ``QoTResultStore`` — it holds no clone/diff/freeze surface.

    There is deliberately NO invalidation: a mutated span (e.g. an
    ``inject_degradation`` NF delta) yields a different key, so a stale entry is
    simply never hit again and ages out via the bounded LRU. The one correctness
    invariant is fingerprint completeness — if the key omits a GSNR input, a hit
    returns a confident wrong number.
    """

    def __init__(self, maxsize: int = 4096) -> None:
        self._d: "OrderedDict[Any, Tuple[QoTState, QoTBreakdown]]" = OrderedDict()
        self._max = maxsize
        self.hits = 0
        self.misses = 0

    def get(self, key: Any) -> Optional[Tuple[QoTState, QoTBreakdown]]:
        hit = self._d.get(key)
        if hit is None:
            self.misses += 1
            return None
        self._d.move_to_end(key)          # LRU: mark most-recently used
        self.hits += 1
        return hit

    def put(self, key: Any, value: Tuple[QoTState, QoTBreakdown]) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)   # evict least-recently used


class HarvestCache:
    """Bounded LRU of full-comb harvest vectors, keyed by adapter.harvest_cache_key
    (path fingerprint + direction + mode — no probe frequency). Off-model, injected
    like QoTCache. Content-addressed: a changed physical input flips the key, so
    there is no invalidation logic."""

    def __init__(self, maxsize: int = 4096) -> None:
        self._store: "OrderedDict[Any, Dict[int, Any]]" = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: Any) -> Optional[Dict[int, Any]]:
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def put(self, key: Any, value: Dict[int, Any]) -> None:
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)
