from pathlib import Path
from gnpy.tools.json_io import load_equipment, load_network


REPO_ROOT = Path(__file__).resolve().parents[2]
EQPT = REPO_ROOT / "eqpt" / "eqpt_config.json"
TOPO = REPO_ROOT / "topologies" / "toy_2span.json"


def test_toy_topology_loads_with_advanced_amp_model():
    eqpt = load_equipment(EQPT)
    network = load_network(TOPO, eqpt)
    from gnpy.core.elements import Edfa
    amps = [n for n in network.nodes if isinstance(n, Edfa)]
    assert amps
    for amp in amps:
        assert amp.params.type_variety != "variable_gain"


# ---------------------------------------------------------------------------
# Task 11: OMS→uids resolver + LoadingState→SI builder
# ---------------------------------------------------------------------------

from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.translate import (
    build_si_for_loading, resolve_oms_path_to_uids,
)


def _toy_model_oms():
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="400G@7.1dB", bitrate_gbps=400.0,
                        required_gsnr_db=7.1, symbol_rate_baud=87.5e9,
                        channel_spacing_hz=100e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_roadm(ROADM(id="ROADM A", target_pch_out_db=-20.0))
    n.add_amplifier(Amplifier(id="booster A",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_amplifier(Amplifier(id="east fiber A to ILA",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_amplifier(Amplifier(id="east edfa in ILA",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_amplifier(Amplifier(id="east fiber ILA to Z",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_amplifier(Amplifier(id="east edfa at Z",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms-AZ", src_node_id="trx A", dst_node_id="trx Z",
                  elements=(
                      "ROADM A",
                      "booster A",
                      "east fiber A to ILA",
                      "east edfa in ILA",
                      "east fiber ILA to Z",
                      "east edfa at Z",
                  )))
    return n


def test_resolve_single_oms_returns_its_elements():
    n = _toy_model_oms()
    assert resolve_oms_path_to_uids(n, ("oms-AZ",)) == (
        "ROADM A",
        "booster A",
        "east fiber A to ILA",
        "east edfa in ILA",
        "east fiber ILA to Z",
        "east edfa at Z",
    )


def test_resolve_unknown_oms_raises():
    n = _toy_model_oms()
    import pytest
    with pytest.raises(KeyError):
        resolve_oms_path_to_uids(n, ("oms-nope",))


def test_build_si_for_single_channel():
    loading = LoadingState(channels=(
        Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),
    ))
    si = build_si_for_loading(loading, baud_rate=87.5e9,
                              roll_off=0.15, tx_osnr=40.0)
    # Check carrier count - actual API varies by gnpy version
    carriers = list(si.carriers) if hasattr(si, "carriers") else list(si)
    assert len(carriers) == 1


def test_build_si_empty_loading_returns_empty_si():
    si = build_si_for_loading(LoadingState.empty(), baud_rate=87.5e9,
                              roll_off=0.15, tx_osnr=40.0)
    carriers = list(si.carriers) if hasattr(si, "carriers") else list(si)
    assert len(carriers) == 0
