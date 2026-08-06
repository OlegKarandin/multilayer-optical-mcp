import json
import tempfile
from pathlib import Path

import pytest

from multilayer_optical_mcp.gnpy_adapter import synthesize as S
from multilayer_optical_mcp.model.assets import (
    Amplifier, Fiber, FiberType, OMS, ROADM, Transceiver, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel

MODE = TransceiverMode(id="400G@7.1dB", bitrate_gbps=400.0, required_gsnr_db=7.1,
                       symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)


def _toy_model() -> NetworkModel:
    """Two-span toy identical to test_ground_truth_bridge._toy_model_synthesized."""
    n = NetworkModel(modes=ModeRegistry([MODE]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_roadm(ROADM(id="roadm_A"))
    n.add_roadm(ROADM(id="roadm_Z"))
    n.add_transceiver(Transceiver(id="trx_A", site="A"))
    n.add_transceiver(Transceiver(id="trx_Z", site="Z"))
    n.add_amplifier(Amplifier(id="amp_booster", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber_0", a_end="roadm_A", z_end="amp_ila",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp_ila", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber_1", a_end="amp_ila", z_end="amp_preamp",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp_preamp", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms_syn", src_node_id="A", dst_node_id="Z", elements=(
        "roadm_A", "amp_booster", "fiber_0", "amp_ila",
        "fiber_1", "amp_preamp")))
    return n


def test_build_leaves_no_temp_dirs(monkeypatch):
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*a, **k):
        p = real_mkdtemp(*a, **k)
        created.append(Path(p))
        return p

    monkeypatch.setattr(tempfile, "mkdtemp", spy_mkdtemp)
    S.clear_network_cache()  # guarantee a fresh synthesis: this test counts them
    model = _toy_model()
    for _ in range(3):
        S.build_gnpy_network(model)

    assert created, "expected build_gnpy_network to create at least one temp dir"
    still_there = [p for p in created if p.exists()]
    assert not still_there, f"orphaned temp dirs left behind: {still_there}"


def test_fingerprint_stable_across_qot_mutation():
    from multilayer_optical_mcp.model.qot import QoTState
    from multilayer_optical_mcp.model.assets import Lightpath

    model = _toy_model()
    model.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_syn",),
                                  mode_id=MODE.id, center_freq_hz=193.4e12))
    fp_before = S._physical_fingerprint(model)
    # A QoT write is NOT physical state — must not change the fingerprint.
    model.set_qot_state("lp0", QoTState(gsnr_db=18.0, osnr_db=25.0,
                                        margin_db=10.9, limiting_element_id=None))
    assert S._physical_fingerprint(model) == fp_before


def test_fingerprint_changes_on_nf_delta():
    model = _toy_model()
    fp_before = S._physical_fingerprint(model)
    model.apply_nf_delta("amp_ila", 2.0)
    assert S._physical_fingerprint(model) != fp_before


def test_repeated_build_returns_same_objects():
    model = _toy_model()
    eq1, net1 = S.build_gnpy_network(model)
    eq2, net2 = S.build_gnpy_network(model)
    assert eq1 is eq2
    assert net1 is net2


def test_physical_mutation_rebuilds():
    model = _toy_model()
    _eq1, net1 = S.build_gnpy_network(model)
    model.apply_nf_delta("amp_ila", 2.0)
    _eq2, net2 = S.build_gnpy_network(model)
    assert net2 is not net1


def test_clone_with_unchanged_physical_layer_reuses_cached_network():
    """The actual fix: validate_plan/commit_plan/branch exploration always
    clone() before mutating lightpaths/services -- a DIFFERENT NetworkModel
    OBJECT, but (for the common case: provision/teardown/set_modulation_format/
    reroute, none of which touch the physical registries) an IDENTICAL physical
    fingerprint. Keying the cache by fingerprint rather than object identity
    means the clone must reuse its parent's already-built network instead of
    resynthesizing from scratch -- this is what makes gating a single-op
    mutation with a validate_plan-equivalent check cost-viable."""
    model = _toy_model()
    eq1, net1 = S.build_gnpy_network(model)

    clone = model.clone()
    from multilayer_optical_mcp.model.assets import Lightpath
    clone.add_lightpath(Lightpath(id="lp0", oms_sequence=("oms_syn",),
                                  mode_id=MODE.id, center_freq_hz=193.4e12))
    assert S._physical_fingerprint(clone) == S._physical_fingerprint(model)

    eq2, net2 = S.build_gnpy_network(clone)
    assert eq2 is eq1
    assert net2 is net1


def test_clone_with_changed_physical_layer_does_not_reuse_cached_network():
    model = _toy_model()
    _eq1, net1 = S.build_gnpy_network(model)

    clone = model.clone()
    clone.apply_nf_delta("amp_ila", 2.0)
    assert S._physical_fingerprint(clone) != S._physical_fingerprint(model)

    _eq2, net2 = S.build_gnpy_network(clone)
    assert net2 is not net1


def test_cached_reuse_matches_fresh_gsnr_no_drift():
    """A heavy loading then a light probe on the SAME (cached) network must give
    the identical probe GSNR a fresh build would — proving effective_gain is reset."""
    from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
    from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
    from multilayer_optical_mcp.model.assets import Direction
    from multilayer_optical_mcp.model.qot_results import QoTResultStore

    probe = Channel(193.4e12, 100e9, None, MODE.id)
    heavy = LoadingState(channels=tuple(
        Channel(193.0e12 + i * 100e9, 100e9, None, MODE.id) for i in range(6)))
    light = LoadingState(channels=(probe,))

    # Fresh model, single light computation = ground truth.
    fresh = _toy_model()
    store_f = QoTResultStore()
    g_fresh, _ = compute_qot(model=fresh, store=store_f, oms_sequence=("oms_syn",),
                             direction=Direction.FORWARD, mode_id=MODE.id,
                             loading=light, center_freq_hz=193.4e12)

    # Same model reused: heavy first (ratchets gains), then the light probe.
    reused = _toy_model()
    store_r = QoTResultStore()
    compute_qot(model=reused, store=store_r, oms_sequence=("oms_syn",),
                direction=Direction.FORWARD, mode_id=MODE.id, loading=heavy,
                center_freq_hz=193.4e12)
    g_reuse, _ = compute_qot(model=reused, store=store_r, oms_sequence=("oms_syn",),
                             direction=Direction.FORWARD, mode_id=MODE.id,
                             loading=light, center_freq_hz=193.4e12)

    assert g_reuse.gsnr_db == g_fresh.gsnr_db, (
        f"cached reuse drifted: reuse={g_reuse.gsnr_db} fresh={g_fresh.gsnr_db}")


def _toy_model_with_lightpaths(n_lp: int) -> NetworkModel:
    from multilayer_optical_mcp.model.assets import Lightpath
    # recompute_qot_under_loading evaluates BOTH directions per lightpath, and
    # backward QoT requires a physically-separate paired reverse OMS (C2). The
    # plan's forward-only _toy_model has no reverse OMS, so build on the
    # bidirectional toy from test_compute_qot (the same one test_recompute_
    # under_loading imports). One model instance = one build = one synthesis, so
    # the "synthesize once" assertion is unaffected.
    from tests.gnpy_adapter.test_compute_qot import _toy_model as _bidi_toy_model
    model = _bidi_toy_model()
    for i in range(n_lp):
        model.add_lightpath(Lightpath(
            id=f"lp{i}", oms_sequence=("oms-AZ",), mode_id="400G@7.1dB",
            center_freq_hz=193.0e12 + i * 100e9))
    return model


def test_bulk_recompute_synthesizes_once(monkeypatch):
    import tempfile
    from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading
    from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
    from multilayer_optical_mcp.model.qot_results import QoTResultStore

    calls = {"topology": 0}
    real_topology = S.model_to_gnpy_topology

    def spy_topology(model):
        calls["topology"] += 1
        return real_topology(model)

    monkeypatch.setattr(S, "model_to_gnpy_topology", spy_topology)

    created = []
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp",
                        lambda *a, **k: created.append(Path(real_mkdtemp(*a, **k)))
                        or str(created[-1]))

    S.clear_network_cache()  # guarantee a fresh synthesis: this test counts them
    model = _toy_model_with_lightpaths(4)
    store = QoTResultStore()
    loading = LoadingState(channels=tuple(
        Channel(193.0e12 + i * 100e9, 100e9, None, MODE.id) for i in range(4)))

    recompute_qot_under_loading(model=model, store=store, loading=loading)

    assert calls["topology"] == 1, (
        f"expected exactly one synthesis across the bulk recompute, "
        f"got {calls['topology']}")
    assert not [p for p in created if p.exists()], "orphaned temp dirs after recompute"
