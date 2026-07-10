# GROUND TRUTH (gnpy==2.14.0, symmetric toy_2span.json, 400G@7.1dB @ 193.4 THz):
# Topology: trx A → ROADM A → booster → fiber(80km) → ILA → fiber(80km) → preamp → ROADM Z → trx Z
# Both ends terminate at a ROADM (S3-11 Option B), so each direction carries one
# add + one drop add_drop_osnr=33 dB penalty (plus tx_osnr=35 dB, OpenROADM v4/v5).
# GSNR: fwd ~17.81 dB, bwd ~17.81 dB (symmetric, was 18.85/17.53 pre-drop-ROADM).
# Updates on intentional gnpy bumps only.

import math

from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import (
    Amplifier,
    Direction,
    Fiber,
    FiberType,
    OMS,
    ROADM,
    Transceiver,
    TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot_results import QoTResultStore


def _toy_model():
    reg = ModeRegistry([
        TransceiverMode(
            id="400G@7.1dB",
            bitrate_gbps=400.0,
            required_gsnr_db=7.1,
            symbol_rate_baud=87.5e9,
            channel_spacing_hz=100e9,
        ),
        TransceiverMode(
            id="800G@15.1dB",
            bitrate_gbps=800.0,
            required_gsnr_db=15.1,
            symbol_rate_baud=87.5e9,
            channel_spacing_hz=100e9,
        ),
        TransceiverMode(
            id="impossible",
            bitrate_gbps=1.0,
            required_gsnr_db=100.0,
            symbol_rate_baud=87.5e9,
            channel_spacing_hz=100e9,
        ),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    # S3-11 Option B: both ends terminate at a ROADM (roadm_<id>) with a
    # registered transceiver — no bare line-terminal transceiver.
    n.add_roadm(ROADM(id="roadm_A", target_pch_out_db=-20.0))
    n.add_transceiver(Transceiver(id="trx_A", site="A"))
    n.add_amplifier(Amplifier(
        id="booster A",
        type_variety="advanced_toy",
        gain_db=20.0,
        nf_db=5.5,
    ))
    n.add_fiber(Fiber(
        id="east fiber A to ILA",
        a_end="roadm_A",
        z_end="east edfa in ILA",
        length_km=80.0,
        type_variety="SSMF",
    ))
    n.add_amplifier(Amplifier(
        id="east edfa in ILA",
        type_variety="advanced_toy",
        gain_db=20.0,
        nf_db=5.5,
    ))
    n.add_fiber(Fiber(
        id="east fiber ILA to Z",
        a_end="east edfa in ILA",
        z_end="east edfa at Z",
        length_km=80.0,
        type_variety="SSMF",
    ))
    n.add_amplifier(Amplifier(
        id="east edfa at Z",
        type_variety="advanced_toy",
        gain_db=20.0,
        nf_db=5.5,
    ))
    n.add_oms(OMS(
        id="oms-AZ",
        src_node_id="A",
        dst_node_id="Z",
        elements=(
            "roadm_A",
            "booster A",
            "east fiber A to ILA",
            "east edfa in ILA",
            "east fiber ILA to Z",
            "east edfa at Z",
        ),
    ))
    # Physically separate reverse OMS (Z -> A) so backward QoT walks its own amp
    # chain and add-side ROADM, not a reversed copy of the forward element list.
    # Symmetric to the forward span: backward QoT ~ forward QoT until an
    # asymmetric per-direction impairment is injected.
    n.add_roadm(ROADM(id="roadm_Z", target_pch_out_db=-20.0))
    n.add_transceiver(Transceiver(id="trx_Z", site="Z"))
    n.add_amplifier(Amplifier(id="booster Z", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="west fiber Z to ILA", a_end="roadm_Z",
                      z_end="west edfa in ILA", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="west edfa in ILA", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="west fiber ILA to A", a_end="west edfa in ILA",
                      z_end="west edfa at A", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="west edfa at A", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(
        id="oms-ZA",
        src_node_id="Z",
        dst_node_id="A",
        elements=(
            "roadm_Z",
            "booster Z",
            "west fiber Z to ILA",
            "west edfa in ILA",
            "west fiber ILA to A",
            "west edfa at A",
        ),
    ))
    return n


def test_compute_qot_returns_state_and_result_id():
    n = _toy_model()
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, None, "400G@7.1dB"),))
    state, rid = compute_qot(
        model=n,
        store=store,
        oms_sequence=("oms-AZ",),
        direction=Direction.FORWARD,
        mode_id="400G@7.1dB",
        loading=loading,
    )
    assert math.isfinite(state.gsnr_db)
    assert math.isfinite(state.osnr_db)
    assert isinstance(state.mode_feasible, bool)
    assert isinstance(rid, str) and rid


def test_breakdown_cached_in_store_with_one_snapshot_per_element():
    n = _toy_model()
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, None, "400G@7.1dB"),))
    _, rid = compute_qot(
        model=n,
        store=store,
        oms_sequence=("oms-AZ",),
        direction=Direction.FORWARD,
        mode_id="400G@7.1dB",
        loading=loading,
    )
    bd = store.get(rid)
    # Six OMS elements + the terminal drop ROADM appended by C2 Step B -> seven.
    assert len(bd.snapshots) == 7
    # Snapshots are labeled with the resolved model uids in order.
    assert bd.snapshots[0].element_id == "roadm_A"
    assert bd.snapshots[-1].element_id == "roadm_Z"


def test_limiting_element_id_is_stable_uid_not_human_string():
    n = _toy_model()
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, None, "400G@7.1dB"),))
    state, rid = compute_qot(
        model=n,
        store=store,
        oms_sequence=("oms-AZ",),
        direction=Direction.FORWARD,
        mode_id="400G@7.1dB",
        loading=loading,
    )
    bd = store.get(rid)
    assert state.limiting_element_id == bd.limiting_element_id
    if state.limiting_element_id is not None:
        valid = {
            "roadm_A",
            "booster A",
            "east fiber A to ILA",
            "east edfa in ILA",
            "east fiber ILA to Z",
            "east edfa at Z",
            "roadm_Z",
        }
        assert state.limiting_element_id in valid


def test_arbitrary_loading_superset_is_evaluated_without_provisioning():
    """Contract: loading may include channels that are never committed.

    A two-channel superset (make-before-break overlap) must propagate cleanly
    and return a finite GSNR for the probe channel.
    """
    n = _toy_model()
    store = QoTResultStore()
    loading = LoadingState(channels=(
        Channel(193.4e12, 100e9, None, "400G@7.1dB"),
        Channel(193.5e12, 100e9, None, "800G@15.1dB"),
    ))
    state, _ = compute_qot(
        model=n,
        store=store,
        oms_sequence=("oms-AZ",),
        direction=Direction.FORWARD,
        mode_id="400G@7.1dB",
        loading=loading,
    )
    assert math.isfinite(state.gsnr_db)


def test_mode_feasible_flips_when_gsnr_below_required():
    """A mode with required_gsnr_db=100 is always infeasible on a real span."""
    n = _toy_model()
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, None, "impossible"),))
    state, _ = compute_qot(
        model=n,
        store=store,
        oms_sequence=("oms-AZ",),
        direction=Direction.FORWARD,
        mode_id="impossible",
        loading=loading,
    )
    assert state.mode_feasible is False
    assert state.margin_db < 0
