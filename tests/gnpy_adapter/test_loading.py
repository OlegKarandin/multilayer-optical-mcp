import pytest
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState


def test_empty_loading():
    assert LoadingState.empty().channels == ()


def test_channel_carries_mode_and_grid_slot():
    ch = Channel(193.4e12, 100e9, 0.0, "400G@7.1dB")
    assert ch.mode_id == "400G@7.1dB"


def test_union_combines_disjoint():
    a = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),))
    b = LoadingState(channels=(Channel(193.5e12, 100e9, 0.0, "300G@4.8dB"),))
    assert len(a.union(b).channels) == 2


def test_union_rejects_spectrum_clash():
    a = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),))
    b = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "300G@4.8dB"),))
    with pytest.raises(ValueError, match="spectrum clash"):
        a.union(b)
