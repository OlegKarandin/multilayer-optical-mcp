import math
from multilayer_optical_mcp.model.assets import Direction
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot, gated_qot
from tests.gnpy_adapter.test_compute_qot import _toy_model


LOADING = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),))


def test_both_directions_return_finite_gsnr():
    """Both propagation directions must return a finite GSNR.

    Topology: trx A → ROADM A → booster A → fiber(80km) → ILA → fiber(80km) → preamp → trx Z
    Forward: booster (G=20 dB) first, then two 16 dB amps.
    Backward (reversed): two 16 dB amps first, then booster (G=20 dB) last.
    In backward the first two amps' ASE is multiplied by G_booster/L_fiber = 2.5×,
    making total backward ASE ≈ 1.66× forward ASE → cascade ~2.2 dB worse GSNR.
    After add_drop_osnr=33 dB + tx_osnr=35 dB penalties: fwd ~18.85 dB, bwd ~17.53 dB.
    Both must complete without error and gated_qot must select the lower of the two.
    """
    n = _toy_model(); store = QoTResultStore()
    fwd, _ = compute_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                         direction=Direction.FORWARD,
                         mode_id="400G@7.1dB", loading=LOADING)
    bwd, _ = compute_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                         direction=Direction.BACKWARD,
                         mode_id="400G@7.1dB", loading=LOADING)
    assert math.isfinite(fwd.gsnr_db)
    assert math.isfinite(bwd.gsnr_db)
    # Backward (booster-last) is the harder path: earlier amps' ASE gets amplified by booster.
    assert bwd.gsnr_db < fwd.gsnr_db


def test_gated_qot_returns_worse_of_two_directions(monkeypatch):
    from multilayer_optical_mcp.gnpy_adapter import adapter as adapter_mod

    calls = []
    def fake(**kwargs):
        calls.append(kwargs["direction"])
        if kwargs["direction"] == Direction.FORWARD:
            return (QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=8.0,
                             limiting_element_id="east fiber A to ILA"),
                    "rid-fwd")
        return (QoTState(gsnr_db=10.0, osnr_db=12.0, margin_db=-2.0,
                         limiting_element_id="east edfa in ILA"),
                "rid-bwd")

    monkeypatch.setattr(adapter_mod, "compute_qot", fake)
    n = _toy_model(); store = QoTResultStore()
    state, rid = gated_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                           mode_id="400G@7.1dB", loading=LOADING)
    assert state.gsnr_db == 10.0
    assert state.mode_feasible is False
    assert state.limiting_element_id == "east edfa in ILA"
    assert rid == "rid-bwd"
    assert set(calls) == {Direction.FORWARD, Direction.BACKWARD}
