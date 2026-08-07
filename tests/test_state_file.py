"""Operating-state file: fingerprint, dump, load, and the composed loader."""
import json

from multilayer_optical_mcp.state_file import FORMAT_VERSION, topology_fingerprint

TOPOLOGY = {
    "graph": {
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "edges": [
            {"src": "a", "dst": "b", "length_km": 80.0},
            {"src": "b", "dst": "c", "length_km": 80.0},
            {"src": "c", "dst": "a", "length_km": 80.0},
        ],
    },
    "srlgs": [{"id": "srlg_ab", "asset_ids": ["roadm_a", "roadm_b"]}],
}


def test_fingerprint_is_stable_and_prefixed():
    fp = topology_fingerprint(TOPOLOGY)
    assert fp.startswith("sha256:")
    assert fp == topology_fingerprint(json.loads(json.dumps(TOPOLOGY)))


def test_fingerprint_ignores_key_order():
    reordered = {"srlgs": TOPOLOGY["srlgs"], "graph": TOPOLOGY["graph"]}
    assert topology_fingerprint(reordered) == topology_fingerprint(TOPOLOGY)


def test_fingerprint_ignores_keys_outside_graph_and_srlgs():
    extra = dict(TOPOLOGY, comment="hand-authored 2026-08-07")
    assert topology_fingerprint(extra) == topology_fingerprint(TOPOLOGY)


def test_fingerprint_changes_when_an_edge_changes():
    changed = json.loads(json.dumps(TOPOLOGY))
    changed["graph"]["edges"][0]["length_km"] = 81.0
    assert topology_fingerprint(changed) != topology_fingerprint(TOPOLOGY)


def test_fingerprint_changes_when_an_srlg_changes():
    changed = json.loads(json.dumps(TOPOLOGY))
    changed["srlgs"][0]["asset_ids"].append("roadm_c")
    assert topology_fingerprint(changed) != topology_fingerprint(TOPOLOGY)


def test_format_version_is_one():
    assert FORMAT_VERSION == 1
