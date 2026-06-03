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

    The toy topology is a unidirectional amp-at-span-end chain, so forward and
    backward GSNRs are NOT expected to match — the amp-first backward path gives
    a much higher GSNR than the fiber-first forward path.  What matters is that
    both directions complete without error and return a finite (non-inf) result,
    and that gated_qot selects the worse (lower) of the two.
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
    # Forward (fiber-first) is the harder path on this topology.
    assert fwd.gsnr_db < bwd.gsnr_db


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
