"""Load-bearing integration test: real-GNPy QoT over a 2-route topology.

Proves that compute_qot can evaluate a *routed* OMS path against a gnpy network
with two parallel routes (north 80 km spans, south 100 km spans) before the
solvers are built on top. The longer (south) route must show the lower GSNR.
"""
import math
from pathlib import Path

from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import (
    Amplifier, Direction, Fiber, FiberType, OMS, ROADM, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot_results import QoTResultStore

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPO_2ROUTE = REPO_ROOT / "topologies" / "toy_2route.json"


def _two_route_model() -> NetworkModel:
    reg = ModeRegistry([
        TransceiverMode(id="400G@7.1dB", bitrate_gbps=400.0, required_gsnr_db=7.1,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_roadm(ROADM(id="ROADM A", target_pch_out_db=-20.0))
    n.add_roadm(ROADM(id="ROADM Z", target_pch_out_db=-20.0))
    for amp_id in ("booster N", "north edfa in ILA", "north preamp at Z",
                   "booster S", "south edfa in ILA", "south preamp at Z"):
        n.add_amplifier(Amplifier(id=amp_id, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="north fiber A to ILA", a_end="ROADM A",
                      z_end="north edfa in ILA", length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="north fiber ILA to Z", a_end="north edfa in ILA",
                      z_end="north preamp at Z", length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="south fiber A to ILA", a_end="ROADM A",
                      z_end="south edfa in ILA", length_km=100.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="south fiber ILA to Z", a_end="south edfa in ILA",
                      z_end="south preamp at Z", length_km=100.0, type_variety="SSMF"))
    n.add_oms(OMS(id="oms-north", src_node_id="A", dst_node_id="Z", elements=(
        "ROADM A", "booster N", "north fiber A to ILA", "north edfa in ILA",
        "north fiber ILA to Z", "north preamp at Z",
    )))
    n.add_oms(OMS(id="oms-south", src_node_id="A", dst_node_id="Z", elements=(
        "ROADM A", "booster S", "south fiber A to ILA", "south edfa in ILA",
        "south fiber ILA to Z", "south preamp at Z",
    )))
    return n


def _gsnr(model, oms_id, direction=Direction.FORWARD) -> float:
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, None, "400G@7.1dB"),))
    state, _ = compute_qot(
        model=model, store=store, oms_sequence=(oms_id,),
        direction=direction, mode_id="400G@7.1dB", loading=loading,
        topo_path=TOPO_2ROUTE,
    )
    return state.gsnr_db


def test_both_routes_return_finite_gsnr():
    n = _two_route_model()
    assert math.isfinite(_gsnr(n, "oms-north"))
    assert math.isfinite(_gsnr(n, "oms-south"))


def test_longer_south_route_has_lower_gsnr():
    n = _two_route_model()
    assert _gsnr(n, "oms-south") < _gsnr(n, "oms-north")
