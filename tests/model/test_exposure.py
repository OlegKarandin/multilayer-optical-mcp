import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, Router, IPLink, Service,
    RiskGroup, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.exposure import (
    ExposureResult, compute_exposure, service_asset_set,
)


def _model_two_paths() -> NetworkModel:
    """Build a model with two disjoint IP link options (ip1 over oms1,
    ip2 over oms2). Service rides ip1 (working) and ip2 (protection)."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for amp_id in ("ampA1", "ampA2", "ampB1", "ampB2"):
        n.add_amplifier(Amplifier(id=amp_id, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber-north", a_end="ampA1", z_end="ampA2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fiber-south", a_end="ampB1", z_end="ampB2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_oms(OMS(id="oms-north", src_node_id="A", dst_node_id="B",
                  elements=("ampA1", "fiber-north", "ampA2")))
    n.add_oms(OMS(id="oms-south", src_node_id="A", dst_node_id="B",
                  elements=("ampB1", "fiber-south", "ampB2")))
    n.add_lightpath(Lightpath(id="lp-north", oms_sequence=("oms-north",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_lightpath(Lightpath(id="lp-south", oms_sequence=("oms-south",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp-north"))
    n.add_ip_link(IPLink(id="ip2", a_router="R1", z_router="R2",
                         lightpath_id="lp-south"))
    n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                          demand_gbps=10.0,
                          working_path=("ip1",),
                          protection_path=("ip2",)))
    return n


def test_asset_set_includes_all_layers():
    n = _model_two_paths()
    # Working path = ip1 over lp-north over oms-north over {ampA1, fiber-north, ampA2}.
    assets = service_asset_set(n, "svc1", which="working")
    assert "ip1" in assets
    assert "lp-north" in assets
    assert "oms-north" in assets
    assert "fiber-north" in assets
    assert "ampA1" in assets and "ampA2" in assets
    # Should NOT include south path assets.
    assert "fiber-south" not in assets


def test_exposure_neither_path_intersects():
    n = _model_two_paths()
    n.define_risk_group(rg_id="rg-unrelated", asset_ids=("fiber-east",))
    res = compute_exposure(n, "svc1", "rg-unrelated")
    assert isinstance(res, ExposureResult)
    assert res.working_intersects is False
    assert res.protection_intersects is False
    assert res.both_intersect is False
    assert res.working_intersection == ()
    assert res.protection_intersection == ()


def test_exposure_working_only_intersects():
    n = _model_two_paths()
    n.define_risk_group(rg_id="rg-north-aerial", asset_ids=("fiber-north",))
    res = compute_exposure(n, "svc1", "rg-north-aerial")
    assert res.working_intersects is True
    assert res.protection_intersects is False
    assert res.both_intersect is False
    assert res.working_intersection == ("fiber-north",)


def test_exposure_both_paths_intersect_is_the_latent_correlation_case():
    """Scenario 1: working and protection were SRLG-disjoint at design time,
    but a newly injected risk group spans them both (e.g. a storm cone over
    both aerial spans). This is the load-bearing signal."""
    n = _model_two_paths()
    n.define_risk_group(
        rg_id="rg-storm-cone",
        asset_ids=("fiber-north", "fiber-south"),
        metadata={"injected_by": "operator"},
    )
    res = compute_exposure(n, "svc1", "rg-storm-cone")
    assert res.both_intersect is True
    assert set(res.working_intersection) == {"fiber-north"}
    assert set(res.protection_intersection) == {"fiber-south"}


def test_exposure_unknown_assets_in_risk_group_miss_silently():
    """Permissive contract: risk-group asset_ids may reference ids this
    server doesn't own. They never match anything; no error."""
    n = _model_two_paths()
    n.define_risk_group(rg_id="rg-opaque", asset_ids=("duct-7", "pole-13"))
    res = compute_exposure(n, "svc1", "rg-opaque")
    assert res.both_intersect is False


def test_exposure_unknown_service_raises():
    n = _model_two_paths()
    with pytest.raises(KeyError):
        compute_exposure(n, "svc-missing", "any")


def test_exposure_unknown_risk_group_raises():
    n = _model_two_paths()
    with pytest.raises(KeyError):
        compute_exposure(n, "svc1", "rg-missing")


def test_exposure_service_with_no_protection_path():
    """Unrouted protection — protection_intersects is False regardless."""
    n = _model_two_paths()
    # Replace the service with one that has no protection assigned.
    n._services["svc1"] = Service(id="svc1", src_router="R1", dst_router="R2",
                                  demand_gbps=10.0, working_path=("ip1",))
    n.define_risk_group(rg_id="rg-north", asset_ids=("fiber-north",))
    res = compute_exposure(n, "svc1", "rg-north")
    assert res.working_intersects is True
    assert res.protection_intersects is False
    assert res.both_intersect is False
