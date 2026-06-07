from multilayer_optical_mcp.gnpy_adapter.synthesize import (
    model_to_gnpy_topology, model_to_gnpy_equipment, nf_type_variety,
)
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode


def _reg():
    return ModeRegistry([TransceiverMode(id="400G@7.1dB", bitrate_gbps=400.0,
        required_gsnr_db=7.1, symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)])


def _tiny_model():
    return model_from_abstract_graph({
        "nodes": [{"id": 0}, {"id": 1}],
        "edges": [{"src": 0, "dst": 1, "length_km": 160.0, "num_spans": 2,
                   "span_lengths_km": [80.0, 80.0], "fiber_type": "SSMF",
                   "amplifier_nf_db": [5.5, 7.5]}],
    }, modes=_reg())


def test_topology_has_all_element_types():
    topo = model_to_gnpy_topology(_tiny_model())
    types = {e["type"] for e in topo["elements"]}
    assert {"Roadm", "Transceiver", "Edfa", "Fiber"} <= types
    pairs = {(c["from_node"], c["to_node"]) for c in topo["connections"]}
    assert ("trx_0", "roadm_0") in pairs


def test_distinct_nf_gets_distinct_type_variety():
    eqpt = model_to_gnpy_equipment(_tiny_model())
    edfa_varieties = {e["type_variety"] for e in eqpt["Edfa"]}
    assert nf_type_variety(5.5) in edfa_varieties
    assert nf_type_variety(7.5) in edfa_varieties


def test_nf_type_variety_carries_flat_nf_polynomial():
    eqpt = model_to_gnpy_equipment(_tiny_model())
    by_name = {e["type_variety"]: e for e in eqpt["Edfa"]}
    adv = by_name[nf_type_variety(7.5)]
    assert adv["type_def"] == "advanced_model"
    assert adv["advanced_config_from_json"]["nf_fit_coeff"][-1] == 7.5
