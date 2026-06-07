from __future__ import annotations

import math
from typing import List


def split_link_into_spans(
    length_km: float,
    target_span_km: float = 80.0,
    min_span_km: float = 20.0,
) -> List[float]:
    """Split a link into balanced spans near *target_span_km*.

    1. n_min = ceil(length/100), n_max = ceil(length/40), clamped to >= 1.
    2. For each n in [n_min, n_max], span_len = length/n; skip if < min_span_km.
    3. Pick n minimising |span_len - target_span_km|.
    4. Return n equal spans, last adjusted so the sum equals length exactly.
    """
    n_min = max(1, math.ceil(length_km / 100.0))
    n_max = max(n_min, math.ceil(length_km / 40.0))

    best_n = None
    best_dev = float("inf")
    for n in range(n_min, n_max + 1):
        span_len = length_km / n
        if span_len < min_span_km:
            continue
        dev = abs(span_len - target_span_km)
        if dev < best_dev:
            best_dev = dev
            best_n = n
    if best_n is None:
        best_n = 1

    base_len = round(length_km / best_n, 2)
    spans = [base_len] * best_n
    spans[-1] = round(length_km - base_len * (best_n - 1), 2)
    return spans
