import pytest
from multilayer_optical_mcp.model.assets import FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, SRLG, RiskGroup, TransceiverMode
from multilayer_optical_mcp.model.ip_assets import Router, IPLink, Service
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel


def _seed_with_one_iplink() -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    return n


def test_service_rejects_unknown_working_iplink():
    n = _seed_with_one_iplink()
    with pytest.raises(ValueError, match="unknown IP link"):
        n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                              demand_gbps=10.0,
                              working_path=("ip-missing",)))


def test_service_rejects_unknown_protection_iplink():
    n = _seed_with_one_iplink()
    with pytest.raises(ValueError, match="unknown IP link"):
        n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                              demand_gbps=10.0,
                              working_path=("ip1",),
                              protection_path=("ip-missing",)))


def test_service_accepts_empty_paths_for_unrouted_demand():
    """A demand that hasn't been routed yet (no working/protection assigned)
    is legal — services exist before routing solves run."""
    n = _seed_with_one_iplink()
    n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                          demand_gbps=10.0))


def test_service_accepts_valid_paths():
    n = _seed_with_one_iplink()
    n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                          demand_gbps=10.0,
                          working_path=("ip1",),
                          protection_path=("ip1",)))


def test_srlg_get_and_members():
    n = _seed_with_one_iplink()
    n.add_srlg(SRLG(id="srlg-aerial-A", asset_ids=("f1", "amp1")))
    g = n.get_srlg("srlg-aerial-A")
    assert g.asset_ids == ("f1", "amp1")
    assert n.get_srlg_members("srlg-aerial-A") == ("f1", "amp1")


def test_define_risk_group_is_permissive_about_asset_ids():
    """Risk groups are abstract partitions; ids need not be in the model."""
    n = _seed_with_one_iplink()
    rg = n.define_risk_group(
        rg_id="rg-storm-1",
        asset_ids=("f1", "duct-7", "pole-13"),  # last two unknown
        metadata={"source": "operator-injected"},
    )
    assert rg.id == "rg-storm-1"
    assert n.get_risk_group("rg-storm-1").asset_ids == ("f1", "duct-7", "pole-13")


def test_define_risk_group_rejects_duplicate_id():
    n = _seed_with_one_iplink()
    n.define_risk_group(rg_id="rg1", asset_ids=("f1",))
    with pytest.raises(ValueError, match="risk group .* already exists"):
        n.define_risk_group(rg_id="rg1", asset_ids=("amp1",))
