from multilayer_optical_mcp.gnpy_adapter.synthesize import (
    model_to_gnpy_topology, model_to_gnpy_equipment, nf_type_variety,
)
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import (
    Amplifier, Fiber, FiberType, OMS, ROADM, TransceiverMode,
)
from multilayer_optical_mcp.model.network import NetworkModel


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
    import json
    eqpt = model_to_gnpy_equipment(_tiny_model())
    by_name = {e["type_variety"]: e for e in eqpt["Edfa"]}
    adv = by_name[nf_type_variety(7.5)]
    assert adv["type_def"] == "advanced_model"
    # advanced_config_from_json is a file-path string (gnpy 2.14.0 reads it as a
    # path); load the JSON from that file to inspect the NF polynomial.
    adv_cfg = json.loads(open(adv["advanced_config_from_json"]).read())
    assert adv_cfg["nf_fit_coeff"][-1] == 7.5


def test_build_gnpy_network_returns_network_with_named_nodes():
    from multilayer_optical_mcp.gnpy_adapter.synthesize import build_gnpy_network
    eqpt, network = build_gnpy_network(_tiny_model())
    uids = {n.uid for n in network.nodes}
    assert "roadm_0" in uids and "roadm_1" in uids
    assert "trx_0" in uids and "trx_1" in uids
    assert "fiber_0_1_0" in uids


# --- Batch C7 ---------------------------------------------------------------


def _model_with(*, roadm: ROADM, amp: Amplifier,
                fiber_types=(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2),),
                fiber=Fiber(id="f0", a_end="roadm_A", z_end="amp_x",
                            length_km=80.0, type_variety="SSMF")) -> NetworkModel:
    """A minimal single-element-of-each model built directly (bypasses the importer)
    so per-instance ROADM/amp/fiber attributes survive to synthesis."""
    n = NetworkModel(modes=_reg())
    for ft in fiber_types:
        n.register_fiber_type(ft)
    n.add_roadm(roadm)
    n.add_amplifier(amp)
    n.add_fiber(fiber)
    return n


def test_amp_tilt_reaches_operational_tilt_target():
    """S3-6: Amplifier.tilt_db must be emitted as operational.tilt_target."""
    model = _model_with(roadm=ROADM(id="roadm_A"),
                        amp=Amplifier(id="amp_x", type_variety="advanced_toy",
                                      gain_db=20.0, nf_db=5.5, tilt_db=-1.5))
    topo = model_to_gnpy_topology(model)
    edfa = next(e for e in topo["elements"] if e["uid"] == "amp_x")
    assert edfa["operational"]["tilt_target"] == -1.5


def test_roadm_target_pch_out_db_is_per_instance():
    """S3-5: ROADM.target_pch_out_db must reach the topology element, not the
    hardcoded global -20."""
    model = _model_with(roadm=ROADM(id="roadm_A", target_pch_out_db=-17.0),
                        amp=Amplifier(id="amp_x", type_variety="advanced_toy",
                                      gain_db=20.0, nf_db=5.5))
    topo = model_to_gnpy_topology(model)
    roadm = next(e for e in topo["elements"] if e["uid"] == "roadm_A")
    assert roadm["params"]["target_pch_out_db"] == -17.0
