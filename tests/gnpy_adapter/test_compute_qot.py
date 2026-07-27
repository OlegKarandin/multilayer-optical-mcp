# GROUND TRUTH (gnpy==2.14.0, symmetric toy_2span.json, 400G@7.1dB @ 193.4 THz):
# Topology: trx A → ROADM A → booster → fiber(80km) → ILA → fiber(80km) → preamp → ROADM Z → trx Z
# Both ends terminate at a ROADM (S3-11 Option B). Each is a TERMINAL ROADM (one
# add or one drop, never both), so per _apply_penalties' half-budget-corrected
# formula each incurs add_drop_osnr + 10*log10(2) = 33 + 3.01 = 36.01 dB (plus
# tx_osnr=35 dB, OpenROADM v4/v5).
# GSNR: fwd ~18.85 dB, bwd ~18.85 dB (symmetric; was ~17.81 dB pre-Task-2, when
# every propagated ROADM was over-charged the bare combined add_drop_osnr).
# Updates on intentional gnpy bumps only.

import math

import pytest

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


def _toy_model(roll_off: float = 0.15):
    reg = ModeRegistry([
        TransceiverMode(
            id="400G@7.1dB",
            bitrate_gbps=400.0,
            required_gsnr_db=7.1,
            symbol_rate_baud=87.5e9,
            channel_spacing_hz=100e9,
            roll_off=roll_off,
        ),
        TransceiverMode(
            id="800G@15.1dB",
            bitrate_gbps=800.0,
            required_gsnr_db=15.1,
            symbol_rate_baud=87.5e9,
            channel_spacing_hz=100e9,
            roll_off=roll_off,
        ),
        TransceiverMode(
            id="impossible",
            bitrate_gbps=1.0,
            required_gsnr_db=100.0,
            symbol_rate_baud=87.5e9,
            channel_spacing_hz=100e9,
            roll_off=roll_off,
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


def test_probe_roll_off_is_sourced_from_mode_not_hardcoded(monkeypatch):
    """S2-4 follow-up: build_si_for_loading's roll_off scalar must come from the
    probed mode's own TransceiverMode.roll_off, not a bare 0.15 literal that
    ignores which mode is being evaluated."""
    from multilayer_optical_mcp.gnpy_adapter import adapter as _adapter
    from multilayer_optical_mcp.gnpy_adapter.translate import build_si_for_loading as _real

    captured = {}

    def _spy(loading, *, baud_rate, roll_off, **kw):
        captured["roll_off"] = roll_off
        return _real(loading, baud_rate=baud_rate, roll_off=roll_off, **kw)

    monkeypatch.setattr(_adapter, "build_si_for_loading", _spy)

    n = _toy_model(roll_off=0.3)
    store = QoTResultStore()
    loading = LoadingState(channels=(
        Channel(center_freq_hz=193.4e12, slot_width_hz=100e9,
                power_dbm=None, mode_id="400G@7.1dB"),
    ))
    compute_qot(model=n, store=store, oms_sequence=("oms-AZ",),
               direction=Direction.FORWARD, mode_id="400G@7.1dB", loading=loading)

    assert captured["roll_off"] == 0.3


def test_compute_qot_is_order_independent_in_loading_channels():
    """Regression for the audit's wrong-carrier-GSNR Critical finding:
    compute_qot must return the SAME GSNR for the probe channel regardless of
    where it sits in loading.channels, since gnpy's SpectralInformation sorts
    by frequency internally and _find_probe_index must track that sort."""
    n = _toy_model(roll_off=0.15)
    store = QoTResultStore()
    probe = Channel(193.4e12, 100e9, None, "400G@7.1dB")
    neighbor_low = Channel(193.2e12, 100e9, None, "400G@7.1dB")
    neighbor_high = Channel(193.6e12, 100e9, None, "400G@7.1dB")

    # Ascending order (already correct today).
    ascending = LoadingState(channels=(neighbor_low, probe, neighbor_high))
    state_ascending, _ = compute_qot(
        model=n, store=store, oms_sequence=("oms-AZ",), direction=Direction.FORWARD,
        mode_id="400G@7.1dB", loading=ascending, center_freq_hz=probe.center_freq_hz)

    # Probe-first order (allocation.py's actual convention -- this is what
    # broke before the fix).
    probe_first = LoadingState(channels=(probe, neighbor_low, neighbor_high))
    state_probe_first, _ = compute_qot(
        model=n, store=store, oms_sequence=("oms-AZ",), direction=Direction.FORWARD,
        mode_id="400G@7.1dB", loading=probe_first, center_freq_hz=probe.center_freq_hz)

    assert state_ascending.gsnr_db == pytest.approx(state_probe_first.gsnr_db, abs=1e-6)


MODE = "400G@7.1dB"


def _mode() -> TransceiverMode:
    return TransceiverMode(
        id=MODE,
        bitrate_gbps=400.0,
        required_gsnr_db=7.1,
        symbol_rate_baud=87.5e9,
        channel_spacing_hz=100e9,
        roll_off=0.15,
    )


def test_apply_penalties_charges_terminal_roadms_only_at_half_budget():
    """Regression for the audit's ROADM-OSNR-penalty Critical finding.
    Builds a real 2-hop A->M->B topology (roadm_M is propagated as an
    EXPRESS/interior ROADM; roadm_A and roadm_B are terminal). Compares the
    fixed _apply_penalties against a locally-reproduced copy of the OLD
    (buggy) formula run on the exact same real si/elements from one real
    propagation -- so this needs no external gnpy ground truth, only the
    invariant that fixing the formula can only IMPROVE (raise) GSNR/OSNR
    relative to the old, over-penalized one, since express roadm_M's bogus
    penalty is removed and the two terminals gain +10*log10(2) dB each.
    """
    from gnpy.core.elements import Roadm as _GnpyRoadm
    from gnpy.core.utils import lin2db, db2lin, snr_sum
    from multilayer_optical_mcp.gnpy_adapter.adapter import (
        _propagate_loading, _apply_penalties, _extract_gsnr_osnr,
    )

    n = NetworkModel(modes=ModeRegistry([_mode()]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for node in ("A", "M", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_transceiver(Transceiver(id="trx_A", site="A"))
    n.add_transceiver(Transceiver(id="trx_B", site="B"))
    n.add_amplifier(Amplifier(id="boost_AM", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f_AM", a_end="roadm_A", z_end="pre_AM",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="pre_AM", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms_AM", src_node_id="A", dst_node_id="M",
                  elements=("roadm_A", "boost_AM", "f_AM", "pre_AM")))
    n.add_amplifier(Amplifier(id="boost_MB", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f_MB", a_end="roadm_M", z_end="pre_MB",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="pre_MB", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms_MB", src_node_id="M", dst_node_id="B",
                  elements=("roadm_M", "boost_MB", "f_MB", "pre_MB")))

    loading = LoadingState(channels=(Channel(193.4e12, 100e9, None, MODE),
                                     Channel(193.5e12, 100e9, None, MODE)))
    pr = _propagate_loading(n, ("oms_AM", "oms_MB"), Direction.FORWARD,
                            loading, _mode(), probe_idx=0)

    assert pr.uids_list[0] == "roadm_A"
    assert "roadm_M" in pr.roadm_propagated
    assert "roadm_M" not in (pr.uids_list[0], pr.uids_list[-1])

    gsnr0, osnr0 = _extract_gsnr_osnr(pr.si, 0)

    def old_apply_penalties(si, idx, uids_list, elements, roadm_propagated,
                            baud_rate, gsnr_db, osnr_db):
        penalties_noise_lin = 0.0
        for _uid, _el in zip(uids_list, elements):
            if isinstance(_el, _GnpyRoadm) and _uid in roadm_propagated:
                penalties_noise_lin += db2lin(-_el.params.add_drop_osnr)
        tx_osnr_db = float(si.tx_osnr[idx])
        penalties_noise_lin += db2lin(-tx_osnr_db)
        combined_penalty_db = -lin2db(penalties_noise_lin)
        return (float(snr_sum(gsnr_db, baud_rate, combined_penalty_db)),
                float(snr_sum(osnr_db, baud_rate, combined_penalty_db)))

    old_gsnr, old_osnr = old_apply_penalties(
        pr.si, 0, pr.uids_list, pr.elements, pr.roadm_propagated, pr.baud_rate,
        gsnr0, osnr0)
    new_gsnr, new_osnr = _apply_penalties(
        pr.si, 0, pr.uids_list, pr.elements, pr.roadm_propagated, pr.baud_rate,
        gsnr0, osnr0)

    assert new_gsnr > old_gsnr
    assert new_osnr > old_osnr
