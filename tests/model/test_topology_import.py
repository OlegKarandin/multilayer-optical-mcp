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


from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode

REPO_ROOT = Path(__file__).resolve().parents[2]
GERMAN_17 = REPO_ROOT / "topologies" / "german_17.json"


def _reg():
    return ModeRegistry([
        TransceiverMode(id="400G@7.1dB", bitrate_gbps=400.0, required_gsnr_db=7.1,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    ])


def _tiny_graph():
    return {
        "nodes": [{"id": 0}, {"id": 1}],
        "edges": [{
            "src": 0, "dst": 1, "length_km": 160.0, "num_spans": 2,
            "span_lengths_km": [80.0, 80.0], "fiber_type": "SSMF",
            "amplifier_nf_db": [5.5, 5.5],
        }],
    }


def test_import_tiny_graph_builds_optical_layer():
    n = model_from_abstract_graph(_tiny_graph(), modes=_reg())
    assert set(n._roadms) == {"roadm_0", "roadm_1"}
    assert set(n._transceivers) == {"trx_0", "trx_1"}
    assert {r.id for r in n.list_oms()} == {"oms_0_1", "oms_1_0"}
    oms = n.get_oms("oms_0_1")
    assert oms.src_node_id == "0" and oms.dst_node_id == "1"
    assert oms.elements[0] == "roadm_0"
    assert oms.elements[1] == "amp_0_1_booster"
    assert oms.elements[2] == "fiber_0_1_0"
    assert oms.elements[3] == "amp_0_1_0"
    assert oms.elements[4] == "fiber_0_1_1"
    assert oms.elements[5] == "amp_0_1_1"
    assert n.get_fiber("fiber_0_1_0").length_km == 80.0
    assert n.get_amplifier("amp_0_1_0").nf_db == 5.5


def test_import_german_17_structural_counts():
    graph = json.loads(GERMAN_17.read_text())
    n = model_from_abstract_graph(graph, modes=_reg())
    assert len(n.list_oms()) == 2 * len(graph["edges"])
    total_spans = sum(e["num_spans"] for e in graph["edges"])
    assert len(n._fibers) == 2 * total_spans
    assert len(n._routers) == len(graph["nodes"])
