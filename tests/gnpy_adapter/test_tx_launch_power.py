"""Step TX (S2-3 + S3-9): the transponder launch power (which sets the TX-OSNR
noise floor, noise_tx = tx_power / tx_osnr_linear) is a separate concept from the
per-channel fiber-input power (pch, = the ROADM target_pch_out ~ -20 dBm).

build_si_for_loading must build gnpy's `tx_power` from a dedicated
`tx_launch_power_dbm` (default 0 dBm, the standard coherent transponder
reference), NOT from the -20 dBm pch default which made the TX-OSNR budget 20 dB
too optimistic.
"""
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.translate import build_si_for_loading

LOADING = LoadingState((Channel(193.4e12, 100e9, 0.0, "M"),))


def test_tx_power_defaults_to_launch_power_not_pch():
    si = build_si_for_loading(LOADING, baud_rate=87.5e9, roll_off=0.15)
    # tx_power (launch noise floor) defaults to 0 dBm = 1e-3 W ...
    assert abs(float(si.tx_power[0]) - 1e-3) < 1e-12, (
        f"tx_power must be the 0 dBm launch reference, got {float(si.tx_power[0]):.3e} W"
    )
    # ... while the fiber-input signal power stays at the -20 dBm pch default.
    assert abs(float(si.signal[0]) - 1e-5) < 1e-12


def test_tx_power_scales_with_launch_param_independent_of_pch():
    si_a = build_si_for_loading(LOADING, baud_rate=87.5e9, roll_off=0.15,
                                tx_power_dbm=-20.0, tx_launch_power_dbm=-10.0)
    si_b = build_si_for_loading(LOADING, baud_rate=87.5e9, roll_off=0.15,
                                tx_power_dbm=-5.0, tx_launch_power_dbm=-10.0)
    # Changing pch (tx_power_dbm) leaves the TX noise floor untouched ...
    assert float(si_a.tx_power[0]) == float(si_b.tx_power[0])
    # ... and tx_power tracks the launch parameter (-10 dBm = 1e-4 W).
    assert abs(float(si_a.tx_power[0]) - 1e-4) < 1e-12
