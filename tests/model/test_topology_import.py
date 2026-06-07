import math
import json
from pathlib import Path

import pytest

from multilayer_optical_mcp.model.topology_import import split_link_into_spans


def test_split_short_link_single_span():
    assert split_link_into_spans(37.0) == [37.0]


def test_split_balances_near_target():
    spans = split_link_into_spans(278.0, target_span_km=80.0)
    assert len(spans) == 4
    assert all(abs(s - 69.5) < 0.01 for s in spans)


def test_split_sum_is_exact():
    for length in (144.0, 208.0, 316.0, 353.0):
        spans = split_link_into_spans(length)
        assert abs(sum(spans) - length) < 1e-6


def test_split_respects_min_span():
    spans = split_link_into_spans(30.0, target_span_km=80.0, min_span_km=20.0)
    assert spans == [30.0]  # cannot subdivide below min_span_km
