"""Operating-state file: fingerprint, dump, load, and the composed loader."""
import json

import pytest

from multilayer_optical_mcp.gnpy_adapter.loading import LoadingState
from multilayer_optical_mcp.model.assets import Direction, Lightpath
from multilayer_optical_mcp.model.ip_assets import IPLink, Service
from multilayer_optical_mcp.model.ip_routing import simulate_ip_routing
from multilayer_optical_mcp.model.modes import load_modulation_formats
from multilayer_optical_mcp.model.objective import evaluate_objective
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.scenario import build_operating_network
from multilayer_optical_mcp.model.snapshots import diff_models
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.state_file import (
    FORMAT_VERSION,
    StateFileError,
    dump_state,
    load_state,
    topology_fingerprint,
)
from multilayer_optical_mcp.topology_loader import MOD_FORMATS_YAML

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


def _model():
    """Bare 3-node ring, then one lightpath + IP link + service laid on by hand.

    Hand-built rather than driven through build_operating_network: this task
    tests the SERIALIZER, so the fixture must pin exact ids and field values
    the assertions can name. Task 3 adds the realistic packer-built round-trip.
    """
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    m = model_from_abstract_graph(TOPOLOGY["graph"], modes=modes)
    mode_id = m.modes.list()[0].id       # ModeRegistry.list(), not list_modes()
    m.add_lightpath(Lightpath(id="lp_1", oms_sequence=("oms_a_b",),
                              mode_id=mode_id, center_freq_hz=193.1e12))
    m.set_qot_state("lp_1", QoTState(gsnr_db=16.5, osnr_db=30.25, margin_db=1.7,
                                     limiting_element_id="amp_a_b_0"))
    m.add_ip_link(IPLink(id="ipl_1", a_router="router_a", z_router="router_b",
                         lightpath_id="lp_1"))
    m.add_service(Service(id="svc_1", src_router="router_a", dst_router="router_b",
                          demand_gbps=300.0, working_path=("ipl_1",)))
    return m


def test_dump_state_emits_the_four_build_registries():
    doc = dump_state(_model(), fingerprint="sha256:abc", meta={"seed": 0})

    assert doc["format_version"] == FORMAT_VERSION
    assert doc["meta"]["topology_fingerprint"] == "sha256:abc"
    assert doc["meta"]["seed"] == 0
    assert [lp["id"] for lp in doc["lightpaths"]] == ["lp_1"]
    assert doc["lightpaths"][0]["oms_sequence"] == ["oms_a_b"]
    assert doc["ip_links"][0]["lightpath_id"] == "lp_1"
    assert doc["services"][0]["working_path"] == ["ipl_1"]
    assert doc["services"][0]["protection_path"] == []


def test_dump_state_round_trips_every_qot_field():
    # limiting_element_id is the 4th QoTState field and the easy one to drop;
    # diff_models compares whole QoTState objects, so dropping it fails Task 3.
    q = dump_state(_model(), fingerprint="f", meta={})["lightpaths"][0]["qot"]
    assert q == {"gsnr_db": 16.5, "osnr_db": 30.25, "margin_db": 1.7,
                 "limiting_element_id": "amp_a_b_0"}


def test_dump_state_tolerates_a_lightpath_with_no_recorded_qot():
    m = _model()
    mode_id = m.modes.list()[0].id       # ModeRegistry.list(), not list_modes()
    m.add_lightpath(Lightpath(id="lp_2", oms_sequence=("oms_b_c",),
                              mode_id=mode_id, center_freq_hz=193.15e12))
    doc = dump_state(m, fingerprint="f", meta={})
    assert doc["lightpaths"][1]["qot"] is None


def test_dump_state_is_deterministic_for_the_same_model_and_meta():
    m = _model()
    meta = {"seed": 0, "built_at": "2026-08-07T20:00:00Z"}
    first = json.dumps(dump_state(m, fingerprint="f", meta=meta))
    second = json.dumps(dump_state(m, fingerprint="f", meta=meta))
    assert first == second


def test_dump_state_sorts_collections_but_not_paths():
    m = _model()
    mode_id = m.modes.list()[0].id       # ModeRegistry.list(), not list_modes()
    # Insert a lightpath whose id sorts BEFORE the existing one.
    m.add_lightpath(Lightpath(id="lp_0", oms_sequence=("oms_b_c", "oms_c_a"),
                              mode_id=mode_id, center_freq_hz=193.15e12))
    doc = dump_state(m, fingerprint="f", meta={})
    assert [lp["id"] for lp in doc["lightpaths"]] == ["lp_0", "lp_1"]
    # ...but the OMS sequence keeps its physical order, unsorted.
    assert doc["lightpaths"][0]["oms_sequence"] == ["oms_b_c", "oms_c_a"]


class ConstQot:
    """Route-agnostic high GSNR: any path clears the top mode's threshold."""
    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        return QoTState(gsnr_db=16.0, osnr_db=30.0, margin_db=0.0)


def _fake_settle(qot):
    def _settle(work):
        for lp in work.list_lightpaths():
            work.set_qot_state(lp.id, qot(oms_sequence=lp.oms_sequence,
                                          direction=Direction.FORWARD,
                                          mode_id=lp.mode_id,
                                          loading=LoadingState.empty()))
    return _settle


def _bare():
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    return model_from_abstract_graph(TOPOLOGY["graph"], modes=modes)


def _built():
    """A REAL packer-built operating network on the 3-node ring, GNPy-free."""
    qot = ConstQot()
    res = build_operating_network(_bare(), seed=0, qot=qot, target_mean_util=0.5,
                                  max_util_cap=0.95, max_iters=6,
                                  settle=_fake_settle(qot))
    assert res.model.list_services(), "fixture must actually place something"
    return res.model


def _reload(built):
    doc = dump_state(built, fingerprint="f", meta={})
    fresh = _bare()
    load_state(fresh, doc)
    return fresh


def test_round_trip_is_identical_across_every_registry():
    built = _built()
    delta = diff_models(built, _reload(built))
    empty = {"added": (), "removed": (), "modified": ()}
    assert delta == {name: empty for name in delta}, delta


def test_round_trip_preserves_ip_routing_and_objective():
    # Structural equality can still hide a derived-capacity change; assert the
    # two behavioral read paths agree too.
    built = _built()
    reloaded = _reload(built)
    assert simulate_ip_routing(reloaded) == simulate_ip_routing(built)
    assert evaluate_objective(reloaded) == evaluate_objective(built)


def test_load_state_rejects_an_unknown_format_version():
    doc = dump_state(_built(), fingerprint="f", meta={})
    doc["format_version"] = 99
    with pytest.raises(StateFileError) as exc:
        load_state(_bare(), doc)
    assert "99" in str(exc.value)


def test_load_state_rejects_a_lightpath_on_an_unknown_oms():
    doc = dump_state(_built(), fingerprint="f", meta={})
    doc["lightpaths"][0]["oms_sequence"] = ["oms_nope_nope"]
    with pytest.raises(StateFileError) as exc:
        load_state(_bare(), doc)
    assert "oms_nope_nope" in str(exc.value)


def test_load_state_rejects_a_service_on_an_unknown_ip_link():
    doc = dump_state(_built(), fingerprint="f", meta={})
    doc["services"][0]["working_path"] = ["ipl_nope"]
    with pytest.raises(StateFileError) as exc:
        load_state(_bare(), doc)
    assert "ipl_nope" in str(exc.value)


def test_load_state_rejects_a_malformed_record():
    doc = dump_state(_built(), fingerprint="f", meta={})
    del doc["lightpaths"][0]["mode_id"]
    with pytest.raises(StateFileError):
        load_state(_bare(), doc)
