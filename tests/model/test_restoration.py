# tests/model/test_restoration.py
"""compute_restoration: per-service recovery over survivors. Read-only; emits
typed candidates (full + degraded); status solution/partial/no_solution."""
from multilayer_optical_mcp.model.assets import ROADM
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, Router, IPLink, Service,
    TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.restoration import compute_restoration


class FakeQot:
    def __init__(self, gsnr=15.0): self._g = gsnr
    def __call__(self, *, oms_sequence, direction, mode_id, loading):
        return QoTState(gsnr_db=self._g, osnr_db=30.0, margin_db=0.0)


def _diamond() -> NetworkModel:
    """A->B direct working lightpath (lp-direct), plus a survivor detour
    A->M->B with existing lightpaths lp-AM, lp-MB. Service rides lp-direct."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=100e9)]))
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("d1", "d2", "m1", "m2", "n1", "n2"):
        n.add_amplifier(Amplifier(a, "advanced_toy", 20.0, 5.5))
    n.add_fiber(Fiber("fAB", "d1", "d2", 80.0, "SSMF"))
    n.add_fiber(Fiber("fAM", "m1", "m2", 60.0, "SSMF"))
    n.add_fiber(Fiber("fMB", "n1", "n2", 60.0, "SSMF"))
    for node in ("A", "B", "M"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS("oms-AB", "A", "B", ("roadm_A", "d1", "fAB", "d2")))
    n.add_oms(OMS("oms-AM", "A", "M", ("roadm_A", "m1", "fAM", "m2")))
    n.add_oms(OMS("oms-MB", "M", "B", ("roadm_M", "n1", "fMB", "n2")))
    n.add_lightpath(Lightpath("lp-direct", ("oms-AB",), "100G", 193.4e12))
    n.add_lightpath(Lightpath("lp-AM", ("oms-AM",), "100G", 193.4e12))
    n.add_lightpath(Lightpath("lp-MB", ("oms-MB",), "100G", 193.4e12))
    for lp in ("lp-direct", "lp-AM", "lp-MB"):
        n.set_qot_state(lp, QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=3.0))
    n.add_router(Router("RA", "A"))
    n.add_router(Router("RM", "M"))
    n.add_router(Router("RB", "B"))
    n.add_ip_link(IPLink("ip-direct", "RA", "RB", "lp-direct"))
    n.add_ip_link(IPLink("ip-AM", "RA", "RM", "lp-AM"))
    n.add_ip_link(IPLink("ip-MB", "RM", "RB", "lp-MB"))
    n.add_service(Service("svc", "RA", "RB", 50.0,
                          working_path=("ip-direct",), protection_path=()))
    return n


def test_restoration_grooms_over_survivor_detour():
    n = _diamond()
    # fAB failed -> lp-direct down; survivors lp-AM + lp-MB groom A->M->B
    res = compute_restoration(n, FakeQot(), "svc", avoid={"assets": ["fAB"]})
    assert res.status is SolverStatus.SOLUTION
    groom = [c for c in res.candidates if c.lever == "ip_reroute"]
    assert groom and groom[0].reused_lightpaths == ("lp-AM", "lp-MB")
    assert groom[0].restored_gbps == 50.0
    assert groom[0].shortfall_gbps == 0.0


def test_restoration_no_survivor_is_no_solution():
    n = _diamond()
    # fail every fiber on both detour legs and the direct -> nothing survives
    res = compute_restoration(n, FakeQot(), "svc",
                              avoid={"assets": ["fAB", "fAM", "fMB"]})
    assert res.status is SolverStatus.NO_SOLUTION
    assert res.candidates == ()


def test_restoration_degraded_when_survivor_partially_loaded():
    n = _diamond()
    # preload 80G on the M->B survivor leg so its residual is 20G
    n.add_service(Service("bg", "RM", "RB", 80.0, working_path=("ip-MB",)))
    # FakeQot below the 12 dB mode threshold -> the new-lightpath lever is
    # infeasible, isolating the degraded groom so status is PARTIAL (not a full
    # optical recovery). Grooming reuses existing lightpaths and calls no QoT.
    res = compute_restoration(n, FakeQot(10.0), "svc", avoid={"assets": ["fAB"]})
    assert res.status is SolverStatus.PARTIAL
    assert all(c.lever == "ip_reroute" for c in res.candidates)
    groom = res.candidates[0]
    assert groom.restored_gbps == 20.0
    assert groom.shortfall_gbps == 30.0


def _diamond_gap() -> NetworkModel:
    """Like _diamond but with NO existing lightpath on the M->B leg: recovery
    after fAB fails must groom A->M (lp-AM) AND light a new M->B lightpath."""
    m = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=100e9)]))
    m.register_fiber_type(FiberType("SSMF", 0.2))
    for a in ("d1", "d2", "m1", "m2", "n1", "n2"):
        m.add_amplifier(Amplifier(a, "advanced_toy", 20.0, 5.5))
    m.add_fiber(Fiber("fAB", "d1", "d2", 80.0, "SSMF"))
    m.add_fiber(Fiber("fAM", "m1", "m2", 60.0, "SSMF"))
    m.add_fiber(Fiber("fMB", "n1", "n2", 60.0, "SSMF"))
    for node in ("A", "B", "M"):
        m.add_roadm(ROADM(id=f"roadm_{node}"))
    m.add_oms(OMS("oms-AB", "A", "B", ("roadm_A", "d1", "fAB", "d2")))
    m.add_oms(OMS("oms-AM", "A", "M", ("roadm_A", "m1", "fAM", "m2")))
    m.add_oms(OMS("oms-MB", "M", "B", ("roadm_M", "n1", "fMB", "n2")))
    m.add_lightpath(Lightpath("lp-direct", ("oms-AB",), "100G", 193.4e12))
    m.add_lightpath(Lightpath("lp-AM", ("oms-AM",), "100G", 193.4e12))
    for lp in ("lp-direct", "lp-AM"):
        m.set_qot_state(lp, QoTState(gsnr_db=15.0, osnr_db=30.0, margin_db=3.0))
    m.add_router(Router("RA", "A"))
    m.add_router(Router("RM", "M"))
    m.add_router(Router("RB", "B"))
    m.add_ip_link(IPLink("ip-direct", "RA", "RB", "lp-direct"))
    m.add_ip_link(IPLink("ip-AM", "RA", "RM", "lp-AM"))
    m.add_service(Service("svc", "RA", "RB", 50.0,
                          working_path=("ip-direct",), protection_path=()))
    return m


def test_restoration_hybrid_groom_plus_new_lightpath():
    n = _diamond_gap()
    res = compute_restoration(n, FakeQot(15.0), "svc", avoid={"assets": ["fAB"]})
    assert res.status is SolverStatus.SOLUTION
    hyb = [c for c in res.candidates if c.lever == "hybrid"]
    assert hyb, "expected a hybrid groom+new candidate"
    assert hyb[0].reused_lightpaths == ("lp-AM",)
    assert hyb[0].new_lightpaths[0].oms_sequence == ("oms-MB",)
    assert hyb[0].restored_gbps == 50.0


def test_cross_bucket_dedup_ignores_wavelength(monkeypatch):
    """A candidate never commits to a wavelength, so the same physical route must
    not survive as two candidates merely because the groom_or_new and new_only
    passes picked different representative lambdas. The cross-bucket dedup key must
    match place_demands' lambda-free intra-bucket key.

    compute_restoration now delegates to route_service, which owns the harvest
    (_harvest) that calls place_demands -- so the fake is installed on
    route_service's binding of place_demands, not restoration's (restoration no
    longer imports it directly)."""
    from multilayer_optical_mcp.model import route_service as RS
    from multilayer_optical_mcp.model.multilayer_graph import Placement, NewLightpathRun

    def _fake_place(model, g, qot, *, src, dst, demand_gbps, policy, k=8):
        # same physical route (oms-AB), different representative lambda per pass
        lam = 0 if policy == "groom_or_new" else 3
        run = NewLightpathRun(("oms-AB",), lam, "100G", 15.0, 100.0,
                              src_node="A", dst_node="B")
        return [Placement(reused_lightpaths=(), new_lightpaths=(run,),
                          restored_gbps=demand_gbps, shortfall_gbps=0.0)]

    monkeypatch.setattr(RS, "place_demands", _fake_place)
    n = _diamond()
    res = compute_restoration(n, FakeQot(15.0), "svc")
    assert len(res.candidates) == 1, [c.new_lightpaths for c in res.candidates]
