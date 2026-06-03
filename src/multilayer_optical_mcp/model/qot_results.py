from __future__ import annotations
import time
import uuid
from collections import OrderedDict
from typing import Dict, Optional, Tuple
from .qot import QoTBreakdown


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
