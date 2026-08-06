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


def test_roadm_add_drop_osnr_is_per_instance():
    """S3-2 follow-up: ROADM.add_drop_osnr_db must reach the topology element,
    not the hardcoded global 33.0."""
    model = _model_with(roadm=ROADM(id="roadm_A", add_drop_osnr_db=38.0),
                        amp=Amplifier(id="amp_x", type_variety="advanced_toy",
                                      gain_db=20.0, nf_db=5.5))
    topo = model_to_gnpy_topology(model)
    roadm = next(e for e in topo["elements"] if e["uid"] == "roadm_A")
    assert roadm["params"]["add_drop_osnr"] == 38.0


def test_equipment_emits_one_fiber_entry_per_registered_type():
    """S3-1: every registered FiberType must produce its own equipment Fiber entry
    with its dispersion/effective_area/pmd, not just a single hardcoded SSMF."""
    leaf = FiberType(type_variety="LEAF", loss_coef_db_per_km=0.22,
                     dispersion=4.2e-6, effective_area=72e-12, pmd_coef=1.0e-15)
    model = _model_with(
        roadm=ROADM(id="roadm_A"),
        amp=Amplifier(id="amp_x", type_variety="advanced_toy", gain_db=20.0, nf_db=5.5),
        fiber_types=(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2), leaf),
    )
    eqpt = model_to_gnpy_equipment(model)
    by_type = {f["type_variety"]: f for f in eqpt["Fiber"]}
    assert {"SSMF", "LEAF"} <= set(by_type)
    assert by_type["LEAF"]["dispersion"] == 4.2e-6
    assert by_type["LEAF"]["effective_area"] == 72e-12
    assert by_type["LEAF"]["pmd_coef"] == 1.0e-15
    # SSMF ground-truth constants preserved (the pinned values).
    assert by_type["SSMF"]["effective_area"] == 83e-12
    assert by_type["SSMF"]["dispersion"] == 1.67e-05


def test_non_ssmf_fiber_builds_without_keyerror():
    """S3-1: a second fiber variety must not raise KeyError in network_from_json."""
    from multilayer_optical_mcp.gnpy_adapter.synthesize import build_gnpy_network
    model = model_from_abstract_graph({
        "nodes": [{"id": 0}, {"id": 1}],
        "edges": [{"src": 0, "dst": 1, "length_km": 80.0,
                   "span_lengths_km": [80.0], "fiber_type": "LEAF",
                   "amplifier_nf_db": [5.5]}],
    }, modes=_reg())
    eqpt, network = build_gnpy_network(model)  # must not raise
    assert any(n.uid == "fiber_0_1_0" for n in network.nodes)


def test_amps_share_one_documented_edfa_envelope_regardless_of_gain():
    """S3-8: every synthesized advanced_model Edfa carries the SAME gain/power
    envelope (gain_flatmax/gain_min/p_max) regardless of its per-instance gain_db.
    Only NF and tilt vary per amp. Lock the single-envelope assumption as an
    executable invariant and source the numbers from named constants, not literals.
    """
    from multilayer_optical_mcp.gnpy_adapter.synthesize import (
        _EDFA_GAIN_FLATMAX_DB, _EDFA_GAIN_MIN_DB, _EDFA_P_MAX_DBM,
    )
    n = NetworkModel(modes=_reg())
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    # two amps with very different gains — the envelope must not vary with gain.
    n.add_amplifier(Amplifier(id="amp_lo", type_variety="advanced_toy",
                              gain_db=8.0, nf_db=5.5))
    n.add_amplifier(Amplifier(id="amp_hi", type_variety="advanced_toy",
                              gain_db=22.0, nf_db=7.5))
    eqpt = model_to_gnpy_equipment(n)
    envelopes = {
        (e["gain_flatmax"], e["gain_min"], e["p_max"]) for e in eqpt["Edfa"]
    }
    assert envelopes == {
        (_EDFA_GAIN_FLATMAX_DB, _EDFA_GAIN_MIN_DB, _EDFA_P_MAX_DBM)
    }


def test_unresolvable_oms_endpoint_raises():
    """S3-11/S3-4: an OMS endpoint that is neither roadm_<id> nor a registered
    transceiver must raise, not silently synthesize a penalty-free Transceiver."""
    import pytest
    model = _model_with(roadm=ROADM(id="roadm_A"),
                        amp=Amplifier(id="amp_x", type_variety="advanced_toy",
                                      gain_db=20.0, nf_db=5.5))
    # dst "typo_Z" resolves to neither roadm_typo_Z nor a registered transceiver.
    model.add_oms(OMS(id="oms_bad", src_node_id="A", dst_node_id="typo_Z",
                      elements=("roadm_A", "amp_x")))
    with pytest.raises(ValueError):
        model_to_gnpy_topology(model)
