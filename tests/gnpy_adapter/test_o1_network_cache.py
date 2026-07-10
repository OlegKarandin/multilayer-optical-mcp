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
