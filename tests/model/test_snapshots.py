import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
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


def test_snapshot_create_returns_id():
    store = SnapshotStore(initial=_seed())
    assert isinstance(store.create(), str)


def test_branch_is_isolated_from_parent():
    store = SnapshotStore(initial=_seed())
    parent = store.create()
    branch = store.branch(parent)
    store.get(branch).add_amplifier(Amplifier(id="amp-new",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    with pytest.raises(KeyError):
        store.get(parent).get_amplifier("amp-new")
    assert store.get(branch).get_amplifier("amp-new").id == "amp-new"


def test_qot_state_is_carried_into_clone():
    store = SnapshotStore(initial=_seed())
    store.current().set_qot_state("lp1",
        QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=2.5,
                 limiting_element_id="f1"))
    sid = store.create()
    cloned = store.get(sid).get_qot_state("lp1")
    assert cloned.margin_db == 2.5
    assert cloned.limiting_element_id == "f1"


def test_restore_replaces_current():
    store = SnapshotStore(initial=_seed())
    sid = store.create()
    store.current().add_amplifier(Amplifier(id="amp-extra",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    store.restore(sid)
    with pytest.raises(KeyError):
        store.current().get_amplifier("amp-extra")


def test_unknown_id_raises():
    store = SnapshotStore(initial=_seed())
    with pytest.raises(KeyError):
        store.get("nope")


# -- Task 6 diff tests (appended here) --------------------------------------

def test_diff_added_oms():
    store = SnapshotStore(initial=_seed())
    a = store.create()
    store.current().add_oms(OMS(id="oms2", src_node_id="X", dst_node_id="Y",
                                elements=("amp1", "f1", "amp2")))
    b = store.create()
    diff = store.diff(a, b)
    assert "oms2" in diff["oms"]["added"]


def test_diff_modified_qot_state():
    store = SnapshotStore(initial=_seed())
    store.current().set_qot_state("lp1",
        QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=2.5))
    a = store.create()
    store.current().set_qot_state("lp1",
        QoTState(gsnr_db=18.0, osnr_db=20.0, margin_db=0.5))
    b = store.create()
    diff = store.diff(a, b)
    assert "lp1" in diff["qot_state"]["modified"]


def test_diff_modified_lightpath_mode():
    store = SnapshotStore(initial=_seed())
    a = store.create()
    store.current().modes._by_id["50G-BPSK"] = TransceiverMode(
        id="50G-BPSK", bitrate_gbps=50.0, required_gsnr_db=8.0,
        symbol_rate_baud=32e9, channel_spacing_hz=50e9,
    )
    store.current().set_lightpath_mode("lp1", "50G-BPSK")
    b = store.create()
    diff = store.diff(a, b)
    assert "lp1" in diff["lightpaths"]["modified"]
