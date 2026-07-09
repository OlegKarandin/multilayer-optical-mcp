import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel, FrozenModelError
from multilayer_optical_mcp.model.snapshots import SnapshotStore


def _seed() -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                  elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    return n


def test_get_returns_frozen_model_that_rejects_mutation():
    store = SnapshotStore(initial=_seed())
    sid = store.create()
    got = store.get(sid)
    with pytest.raises(FrozenModelError):
        got.add_amplifier(Amplifier(id="amp-x", type_variety="advanced_toy",
                                    gain_db=20.0, nf_db=5.5))


def test_frozen_mutation_attempt_does_not_corrupt_stored_snapshot():
    store = SnapshotStore(initial=_seed())
    sid = store.create()
    with pytest.raises(FrozenModelError):
        store.get(sid).add_amplifier(Amplifier(id="amp-x",
            type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    # Stored snapshot is intact: a fresh get still lacks the asset.
    with pytest.raises(KeyError):
        store.get(sid).get_amplifier("amp-x")


def test_clone_returns_unfrozen_mutable_copy():
    model = _seed()
    clone = model.clone()
    # Clone is independent and mutable.
    clone.add_amplifier(Amplifier(id="amp-x", type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    assert clone.get_amplifier("amp-x").id == "amp-x"
    with pytest.raises(KeyError):
        model.get_amplifier("amp-x")


def test_clone_of_frozen_model_is_unfrozen():
    store = SnapshotStore(initial=_seed())
    sid = store.create()
    frozen = store.get(sid)
    thawed = frozen.clone()
    thawed.add_amplifier(Amplifier(id="amp-x", type_variety="advanced_toy",
                                   gain_db=20.0, nf_db=5.5))
    assert thawed.get_amplifier("amp-x").id == "amp-x"


def test_branch_current_is_unfrozen_and_mutable():
    store = SnapshotStore(initial=_seed())
    parent = store.create()
    store.branch(parent)
    store.current().add_amplifier(Amplifier(id="amp-new",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    assert store.current().get_amplifier("amp-new").id == "amp-new"
