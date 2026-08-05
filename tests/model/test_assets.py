import pytest
from multilayer_optical_mcp.model.assets import FiberType, Fiber, Amplifier, ROADM, Transceiver, TransceiverMode, OMS, Lightpath, SRLG, RiskGroup, Direction
from multilayer_optical_mcp.model.qot import (
    QoTState, ElementSnapshot, QoTBreakdown,
)


def test_fiber_is_frozen_and_carries_only_instance_state():
    f = Fiber(id="f1", a_end="amp-A", z_end="amp-B",
              length_km=80.0, type_variety="SSMF")
    assert f.length_km == 80.0
    assert not hasattr(f, "loss_coef_db_per_km")  # lives on FiberType


def test_fiber_type_carries_loss_and_optical_params():
    ft = FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2)
    assert ft.loss_coef_db_per_km == 0.2
    assert ft.dispersion > 0


def test_transceiver_mode_carries_bitrate_baud_spacing():
    m = TransceiverMode(
        id="400G@7.1dB",
        bitrate_gbps=400.0,
        required_gsnr_db=7.1,
        symbol_rate_baud=87.5e9,
        channel_spacing_hz=100e9,
    )
    assert m.bitrate_gbps == 400.0
    assert m.symbol_rate_baud == 87.5e9


def test_oms_carries_endpoints_and_element_sequence():
    oms = OMS(id="oms-AZ", src_node_id="trx A", dst_node_id="trx Z",
              elements=("amp-1", "fiber-1", "amp-2", "fiber-2"))
    assert len(oms.elements) == 4


def test_lightpath_uses_oms_sequence_no_slot_width_no_margin():
    lp = Lightpath(id="lp1", oms_sequence=("oms-AZ",),
                   mode_id="400G@7.1dB", center_freq_hz=193.4e12)
    assert lp.oms_sequence == ("oms-AZ",)
    assert not hasattr(lp, "slot_width_hz")
    assert not hasattr(lp, "margin_db")
    assert not hasattr(lp, "path")


def test_risk_group_metadata_is_read_only_and_defensively_copied():
    # S1-1: a frozen dataclass carrying a bare dict is only shallow-frozen —
    # rg.metadata[k] = v mutates it. Store a read-only mapping instead.
    rg = RiskGroup(id="rg1", asset_ids=("a",), metadata={"severity": "high"})
    assert rg.metadata["severity"] == "high"
    with pytest.raises(TypeError):
        rg.metadata["severity"] = "low"

    # The mapping passed in must be defensively copied, so a later mutation of
    # the caller's own dict can't leak into the frozen risk group.
    src = {"k": 1}
    rg2 = RiskGroup(id="rg2", asset_ids=(), metadata=src)
    src["k"] = 999
    assert rg2.metadata["k"] == 1

    # Equality against a plain dict / another RiskGroup still holds.
    assert dict(rg.metadata) == {"severity": "high"}
    assert rg == RiskGroup(id="rg1", asset_ids=("a",), metadata={"severity": "high"})


def test_direction_enum():
    assert Direction.FORWARD.value == "forward"
    assert Direction.BACKWARD.value == "backward"


def test_qot_state_carries_limiting_element_and_derived_feasibility():
    ok = QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=3.5,
                  limiting_element_id="east edfa in A to ILA")
    bad = QoTState(gsnr_db=10.0, osnr_db=12.0, margin_db=-0.5,
                   limiting_element_id="east fiber ILA to Z")
    assert ok.mode_feasible is True
    assert bad.mode_feasible is False
    assert bad.limiting_element_id == "east fiber ILA to Z"


def test_qot_state_defaults_limiting_element_to_none():
    s = QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=3.5)
    assert s.limiting_element_id is None


def test_element_snapshot_records_per_element_contribution():
    es = ElementSnapshot(
        element_id="east fiber A to ILA",
        gsnr_db_after=18.0,
        osnr_db_after=20.0,
        gsnr_delta_db=-2.0,
        ase_contribution_db=-1.2,
        nli_contribution_db=-0.8,
    )
    assert es.gsnr_delta_db == -2.0


def test_qot_breakdown_holds_ordered_snapshots_and_limiting_pointer():
    bd = QoTBreakdown(
        snapshots=(
            ElementSnapshot("amp1", 22.0, 23.0, 0.0, 0.0, 0.0),
            ElementSnapshot("fiber1", 19.0, 20.5, -3.0, -1.5, -1.5),
            ElementSnapshot("amp2", 18.5, 20.0, -0.5, -0.5, 0.0),
        ),
        limiting_element_id="fiber1",
    )
    assert bd.snapshots[1].element_id == "fiber1"
    assert bd.limiting_element_id == "fiber1"
