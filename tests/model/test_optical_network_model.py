"""OpticalNetworkModel: the IP-free half of the model.

These tests are the contract for the model split. They prove three things the
rest of the suite cannot:

1. the optical surface works standalone, with no ``NetworkModel`` anywhere;
2. importing ``model.optical_network`` does not pull in a single IP module
   (checked in a fresh subprocess — in-process, pytest has already imported
   everything);
3. ``clone()`` is a genuine template method: a ``NetworkModel`` clone is still a
   ``NetworkModel`` and still carries its routers/IP links/services.
"""

import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import (
    Amplifier,
    Direction,
    Fiber,
    FiberType,
    Lightpath,
    OMS,
    ROADM,
    RiskGroup,
    SRLG,
    Transceiver,
    TransceiverMode,
)
from multilayer_optical_mcp.model.ip_assets import IPLink, Router, Service
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import FrozenModelError, NetworkModel
from multilayer_optical_mcp.model.optical_network import (
    OpticalNetworkModel,
    lightpath_footprint,
)
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore


def _modes() -> ModeRegistry:
    return ModeRegistry([
        TransceiverMode(
            id="400G@7.1dB",
            bitrate_gbps=400.0,
            required_gsnr_db=7.1,
            symbol_rate_baud=87.5e9,
            channel_spacing_hz=100e9,
        ),
    ])


def _toy_optical(cls=OpticalNetworkModel):
    """The symmetric toy_2span topology of tests/gnpy_adapter/test_compute_qot.py,
    built through *cls* — which is either ``OpticalNetworkModel`` or its
    ``NetworkModel`` subclass. Every call below is an inherited optical method,
    which is the point: the builder never touches an IP concept."""
    n = cls(modes=_modes())
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    # East (A -> Z).
    n.add_roadm(ROADM(id="roadm_A", target_pch_out_db=-20.0))
    n.add_transceiver(Transceiver(id="trx_A", site="A"))
    n.add_amplifier(Amplifier(id="booster A", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="east fiber A to ILA", a_end="roadm_A",
                      z_end="east edfa in ILA", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="east edfa in ILA", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="east fiber ILA to Z", a_end="east edfa in ILA",
                      z_end="east edfa at Z", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="east edfa at Z", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(
        id="oms-AZ", src_node_id="A", dst_node_id="Z",
        elements=(
            "roadm_A",
            "booster A",
            "east fiber A to ILA",
            "east edfa in ILA",
            "east fiber ILA to Z",
            "east edfa at Z",
        ),
    ))
    # West (Z -> A): a physically separate reverse OMS with its own amp chain.
    n.add_roadm(ROADM(id="roadm_Z", target_pch_out_db=-20.0))
    n.add_transceiver(Transceiver(id="trx_Z", site="Z"))
    n.add_amplifier(Amplifier(id="booster Z", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="west fiber Z to ILA", a_end="roadm_Z",
                      z_end="west edfa in ILA", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="west edfa in ILA", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="west fiber ILA to A", a_end="west edfa in ILA",
                      z_end="west edfa at A", length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="west edfa at A", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(
        id="oms-ZA", src_node_id="Z", dst_node_id="A",
        elements=(
            "roadm_Z",
            "booster Z",
            "west fiber Z to ILA",
            "west edfa in ILA",
            "west fiber ILA to A",
            "west edfa at A",
        ),
    ))
    return n


# --------------------------------------------------------------- optical surface


def test_optical_model_full_surface_standalone():
    """The whole optical surface, exercised on a bare OpticalNetworkModel — no
    NetworkModel, no routers, no IP links anywhere in this test."""
    n = _toy_optical()
    assert not isinstance(n, NetworkModel)

    # registries populated by the builder
    assert n.get_fiber_type("SSMF").loss_coef_db_per_km == 0.2
    assert n.get_fiber("east fiber A to ILA").length_km == 80.0
    assert n.get_amplifier("booster A").nf_db == 5.5
    assert n.has_roadm("roadm_A") and n.has_roadm("roadm_Z")
    assert {o.id for o in n.list_oms()} == {"oms-AZ", "oms-ZA"}
    assert n.get_oms("oms-AZ").dst_node_id == "Z"

    # lightpaths
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms-AZ",),
                              mode_id="400G@7.1dB", center_freq_hz=193.4e12))
    assert n.get_lightpath("lp1").mode_id == "400G@7.1dB"
    assert len(n.list_lightpaths()) == 1

    # QoT state + mode mutation
    n.set_qot_state("lp1", QoTState(gsnr_db=18.0, osnr_db=20.0, margin_db=10.9))
    assert n.get_qot_state("lp1").margin_db == pytest.approx(10.9)
    n.set_lightpath_mode("lp1", "400G@7.1dB")
    with pytest.raises(LookupError):
        n.get_qot_state("lp1")  # S1-7: mode change invalidates recorded QoT

    # groups
    n.add_srlg(SRLG(id="srlg-east", asset_ids=("east fiber A to ILA",)))
    assert n.get_srlg_members("srlg-east") == ("east fiber A to ILA",)
    n.add_risk_group(RiskGroup(id="rg-static", asset_ids=("roadm_A",)))
    rg = n.define_risk_group("rg-dyn", ("east edfa in ILA",), {"why": "heat"})
    assert rg.metadata["why"] == "heat"
    assert {g.id for g in n.list_srlgs()} == {"srlg-east"}
    assert {g.id for g in n.list_risk_groups()} == {"rg-static", "rg-dyn"}

    # injection mutators
    n.apply_nf_delta("booster A", 2.0)
    assert n.get_amplifier("booster A").nf_db == pytest.approx(7.5)
    n.apply_loss_delta("east fiber A to ILA", 3.0)
    assert n.get_fiber("east fiber A to ILA").extra_loss_db == pytest.approx(3.0)

    # optical-only teardown, idempotent
    n.remove_lightpath("lp1")
    n.remove_lightpath("lp1")
    assert n.list_lightpaths() == ()


def test_optical_model_mark_and_clear_failed():
    """clear_failed is the method that used to carry a hidden IP dependency (a
    lazy ``from .exposure import lightpath_footprint``, and exposure imports
    NetworkModel at module scope). It must work on a bare optical model."""
    n = _toy_optical()
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms-AZ",),
                              mode_id="400G@7.1dB", center_freq_hz=193.4e12))
    sentinel = QoTState(gsnr_db=-math.inf, osnr_db=-math.inf, margin_db=-math.inf)

    n.mark_failed(("east edfa in ILA", "roadm_Z"))
    n.set_qot_state("lp1", sentinel)
    assert n.is_failed("east edfa in ILA")
    assert n.failed_assets() == frozenset({"east edfa in ILA", "roadm_Z"})

    # A remaining failed asset still crosses lp1 -> the sentinel stays.
    n.clear_failed(("roadm_Z",))
    assert n.get_qot_state("lp1").margin_db == -math.inf

    # Nothing failed crosses lp1 any more -> the sentinel is dropped (S8-6).
    n.clear_failed(("east edfa in ILA",))
    assert n.failed_assets() == frozenset()
    with pytest.raises(LookupError):
        n.get_qot_state("lp1")


def test_lightpath_footprint_helpers_available_from_both_modules():
    """The three footprint helpers moved to optical_network; exposure re-exports
    them so its five existing consumers are untouched."""
    from multilayer_optical_mcp.model import exposure

    n = _toy_optical()
    fp = lightpath_footprint(n, ("oms-AZ",))
    assert "oms-AZ" in fp and "east edfa in ILA" in fp
    assert "roadm_Z" in fp  # terminal drop ROADM, omitted from OMS.elements
    assert exposure.lightpath_footprint is lightpath_footprint
    assert exposure.oms_seq_asset_set(n, ("oms-AZ",)) == fp - {"roadm_Z"}
    assert exposure.terminal_roadm_id(n, ("oms-AZ",)) == "roadm_Z"


def test_optical_model_freeze_and_clone():
    n = _toy_optical()
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms-AZ",),
                              mode_id="400G@7.1dB", center_freq_hz=193.4e12))
    n.freeze()
    with pytest.raises(FrozenModelError):
        n.add_lightpath(Lightpath(id="lp2", oms_sequence=("oms-ZA",),
                                  mode_id="400G@7.1dB", center_freq_hz=193.5e12))

    c = n.clone()
    assert type(c) is OpticalNetworkModel
    assert c._frozen is False  # a clone is always unfrozen
    assert {lp.id for lp in c.list_lightpaths()} == {"lp1"}
    # independent collections
    c.add_lightpath(Lightpath(id="lp2", oms_sequence=("oms-ZA",),
                              mode_id="400G@7.1dB", center_freq_hz=193.5e12))
    assert len(n.list_lightpaths()) == 1
    assert len(c.list_lightpaths()) == 2


# --------------------------------------------------------------- import isolation


def test_optical_model_imports_without_ip_layer():
    """The real proof of the split. A fresh subprocess is required: in-process,
    pytest has already imported the whole package."""
    code = (
        "import multilayer_optical_mcp.model.optical_network;"
        "import sys;"
        "bad=[m for m in sys.modules "
        "if m.endswith(('.ip_assets','.network','.ip_routing'))];"
        "assert not bad, bad"
    )
    # The package is imported from the source tree (pytest's pythonpath=src),
    # not installed, so the child needs the same src root on PYTHONPATH.
    import multilayer_optical_mcp
    src_root = str(Path(multilayer_optical_mcp.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [src_root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------- Trap 1: clone


def _ip_model() -> NetworkModel:
    n = _toy_optical(NetworkModel)
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms-AZ",),
                              mode_id="400G@7.1dB", center_freq_hz=193.4e12))
    n.add_router(Router(id="rtr_A", site="A"))
    n.add_router(Router(id="rtr_Z", site="Z"))
    n.add_ip_link(IPLink(id="ip1", a_router="rtr_A", z_router="rtr_Z",
                         lightpath_id="lp1"))
    n.add_service(Service(id="svc1", src_router="rtr_A", dst_router="rtr_Z",
                          demand_gbps=100.0, working_path=("ip1",)))
    return n


def test_network_model_clone_keeps_class_and_ip_state():
    """Trap 1. If OpticalNetworkModel.clone() hardcoded its own class instead of
    ``type(self)``, or NetworkModel failed to extend ``_copy_state_into``, every
    clone would silently drop routers/IP links/services — and SnapshotStore's
    create/branch/get/restore/put all route through clone(), so every snapshot in
    the system would be corrupt. The class check alone does not catch the second
    half, so both are asserted here."""
    n = _ip_model()
    c = n.clone()

    assert type(c) is NetworkModel
    assert {r.id for r in c.list_routers()} == {"rtr_A", "rtr_Z"}
    assert {link.id for link in c.list_ip_links()} == {"ip1"}
    assert {s.id for s in c.list_services()} == {"svc1"}
    assert c.get_ip_link("ip1").lightpath_id == "lp1"
    assert c.get_service("svc1").working_path == ("ip1",)
    # optical state survives too
    assert {lp.id for lp in c.list_lightpaths()} == {"lp1"}

    # and the copies are independent
    c.add_router(Router(id="rtr_B", site="B"))
    assert len(n.list_routers()) == 2
    assert len(c.list_routers()) == 3


def test_frozen_network_model_clone_is_unfrozen_and_complete():
    n = _ip_model().freeze()
    c = n.clone()
    assert type(c) is NetworkModel
    c.add_router(Router(id="rtr_B", site="B"))  # must not raise
    assert {s.id for s in c.list_services()} == {"svc1"}


# --------------------------------------------------------- Trap 2: remove_lightpath


def test_remove_lightpath_is_overridden_and_unbinds_ip_links():
    """Trap 2. remove_lightpath is the one optical mutator that needs IP-side
    cleanup, so it must be genuinely overridden on NetworkModel — silently
    inheriting the optical-only version would leave IP links bound to a
    lightpath that no longer exists."""
    assert "remove_lightpath" in NetworkModel.__dict__

    n = _ip_model()
    assert n.ip_links_for_lightpath("lp1") == ("ip1",)

    n.remove_lightpath("lp1")
    assert n.list_lightpaths() == ()
    assert n.list_ip_links() == ()

    # idempotent: the unknown-id guard must run BEFORE ip_links_for_lightpath,
    # which raises KeyError on an unknown lightpath id.
    n.remove_lightpath("lp1")
    n.remove_lightpath("never-existed")


# ------------------------------------------------- reuse claim: QoT on bare model


def test_compute_qot_matches_between_bare_optical_and_network_model():
    """The concrete evidence for the reuse claim: compute_qot driven by a bare
    OpticalNetworkModel returns exactly the GSNR/margin of the equivalent
    NetworkModel case in tests/gnpy_adapter/test_compute_qot.py (~18.85 dB fwd
    on the symmetric toy_2span topology)."""
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, None, "400G@7.1dB"),))

    optical = _toy_optical(OpticalNetworkModel)
    full = _toy_optical(NetworkModel)

    results = []
    for model in (optical, full):
        state, _rid = compute_qot(
            model=model,
            store=QoTResultStore(),
            oms_sequence=("oms-AZ",),
            direction=Direction.FORWARD,
            mode_id="400G@7.1dB",
            loading=loading,
        )
        results.append(state)

    bare, ref = results
    assert math.isfinite(bare.gsnr_db)
    assert bare.gsnr_db == pytest.approx(ref.gsnr_db, abs=1e-9)
    assert bare.osnr_db == pytest.approx(ref.osnr_db, abs=1e-9)
    assert bare.margin_db == pytest.approx(ref.margin_db, abs=1e-9)
    assert bare.limiting_element_id == ref.limiting_element_id
    assert bare.mode_feasible is ref.mode_feasible
    # ground truth (gnpy==2.14.0, symmetric toy_2span, 400G@7.1dB @ 193.4 THz)
    assert bare.gsnr_db == pytest.approx(18.85, abs=0.3)
