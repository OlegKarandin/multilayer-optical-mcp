import pytest
from multilayer_optical_mcp.model.assets import (
    Fiber, FiberType, Amplifier, Lightpath, Router, IPLink, OMS, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel


def _registry():
    return ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ])


def _bare():
    return NetworkModel(modes=_registry())


def test_fiber_requires_registered_type():
    n = _bare()
    with pytest.raises(ValueError, match="unknown fiber type"):
        n.add_fiber(Fiber(id="f1", a_end="A", z_end="B",
                          length_km=80.0, type_variety="SSMF"))


def test_fiber_accepted_after_type_registered():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    f = Fiber(id="f1", a_end="A", z_end="B", length_km=80.0, type_variety="SSMF")
    n.add_fiber(f)
    assert n.get_fiber("f1") is f
    assert n.get_fiber_type("SSMF").loss_coef_db_per_km == 0.2


def test_oms_requires_existing_elements():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    with pytest.raises(ValueError, match="neither fiber, amplifier, nor roadm"):
        n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                      elements=("amp1", "fiber-missing")))


def test_optical_node_shadow_registry_is_gone():
    # S1-6: the OpticalNode class and its _optical_nodes registry were written,
    # cloned, and never read. Lock the removal so nobody resurrects a second,
    # perpetually-drifting node store alongside _roadms/_transceivers.
    import multilayer_optical_mcp.model.assets as assets
    assert not hasattr(assets, "OpticalNode")
    n = _bare()
    assert not hasattr(n, "add_optical_node")
    assert not hasattr(n, "_optical_nodes")
    assert not hasattr(n.clone(), "_optical_nodes")


def test_lightpath_requires_existing_oms():
    n = _bare()
    with pytest.raises(ValueError, match="unknown OMS"):
        n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms-missing",),
                                  mode_id="100G-QPSK", center_freq_hz=193.4e12))


def test_ip_link_requires_existing_lightpath():
    n = _bare()
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    with pytest.raises(ValueError, match="unknown lightpath"):
        n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                             lightpath_id="lp-missing"))


def test_full_happy_path_add_chain():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="trxA", dst_node_id="trxB",
                  elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    assert n.get_ip_link("ip1").lightpath_id == "lp1"
    assert n.get_oms("oms1").elements == ("amp1", "f1", "amp2")
