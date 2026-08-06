# tests/test_server_phase8.py
"""compute_restoration MCP tool returns a structured candidate list."""
import pytest
from multilayer_optical_mcp.model.restoration import (
    RestorationResult, RestorationCandidate,
)
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.multilayer_graph import NewLightpathRun
from multilayer_optical_mcp.model.views import restoration_result_dict
from multilayer_optical_mcp.model.route_service import (
    RouteServiceResult, RouteServiceCandidate, RoutePair,
)
from multilayer_optical_mcp.model.objective import ObjectiveResult
from multilayer_optical_mcp.model.assets import FiberType
from multilayer_optical_mcp.model.ip_assets import Router, Service
from multilayer_optical_mcp.server import build_app
from tests.phase7_topology import add_bidir_span


def test_restoration_result_dict_shape():
    res = RestorationResult(
        status=SolverStatus.PARTIAL, service_id="svc", demand_gbps=50.0,
        candidates=(
            RestorationCandidate(
                lever="ip_reroute", reused_lightpaths=("lp-AM", "lp-MB"),
                new_lightpaths=(), restored_gbps=20.0, shortfall_gbps=30.0,
                cost_vector={"transponders": 0.0, "new_lightpaths": 0.0, "hops": 2.0}),
            RestorationCandidate(
                lever="optical_reroute", reused_lightpaths=(),
                new_lightpaths=(NewLightpathRun(("oms-AB",), 0, "100G", 15.0, 100.0),),
                restored_gbps=50.0, shortfall_gbps=0.0,
                cost_vector={"transponders": 2.0, "new_lightpaths": 1.0, "hops": 1.0}),
        ),
    )
    d = restoration_result_dict(res)
    assert d["status"] == "partial"
    assert d["service_id"] == "svc"
    assert len(d["candidates"]) == 2
    c0 = d["candidates"][0]
    assert c0["lever"] == "ip_reroute"
    assert c0["reused_lightpaths"] == ["lp-AM", "lp-MB"]
    assert c0["restored_gbps"] == 20.0
    c1 = d["candidates"][1]
    assert c1["new_lightpaths"][0]["oms_sequence"] == ["oms-AB"]
    assert c1["new_lightpaths"][0]["lam"] == 0
    assert c1["new_lightpaths"][0]["bitrate_gbps"] == 100.0


def _call(app, name, **kwargs):
    return app._tool_manager._tools[name].fn(**kwargs)


def _seed(app):
    """Register a synthesizable bidirectional span A<->B plus a routed service,
    so route_service (real GNPy via make_adapter_evaluator) and evaluate_objective
    have something to route/score."""
    n = app._snapshots.current()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    add_bidir_span(n, "A", "B", "omsAB")
    n.add_router(Router(id="RA", site="A"))
    n.add_router(Router(id="RB", site="B"))
    n.add_service(Service(id="svc1", src_router="RA", dst_router="RB",
                          demand_gbps=100.0, working_path=()))
    return n


def test_evaluate_objective_result_dict_shape():
    from multilayer_optical_mcp.model.views import objective_result_dict
    obj = ObjectiveResult(
        spectrum_used=4, transponders=2.0, max_util=0.5, dropped_traffic=0.0,
        added_latency=1.2, total_margin=15.0, services_at_risk=0, scalar=-10.0,
    )
    d = objective_result_dict(obj)
    assert set(d) >= {"spectrum_used", "transponders", "max_util", "dropped_traffic",
                      "added_latency", "total_margin", "services_at_risk", "scalar"}
    assert d["scalar"] == -10.0
    assert d["transponders"] == 2.0


def test_route_service_result_dict_shape():
    from multilayer_optical_mcp.model.views import route_service_result_dict
    cand = RouteServiceCandidate(
        lever="new_lightpath", reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0),),
        restored_gbps=100.0, shortfall_gbps=0.0,
        cost_vector={"transponders": 2.0, "scalar": -5.0})
    res = RouteServiceResult(
        status=SolverStatus.SOLUTION, service_id="svc1", demand_gbps=100.0,
        protected=False, candidates=(cand,), pairs=())
    d = route_service_result_dict(res)
    assert d["status"] == "solution"
    assert d["service_id"] == "svc1"
    assert "candidates" in d
    assert d["candidates"][0]["cost_vector"] == {"transponders": 2.0, "scalar": -5.0}
    assert d["candidates"][0]["new_lightpaths"][0]["oms_sequence"] == ["omsAB"]

    work_cand = RouteServiceCandidate(
        lever="optical_reroute", reused_lightpaths=(),
        new_lightpaths=(NewLightpathRun(("omsAB",), 0, "100G", 15.0, 100.0),),
        restored_gbps=100.0, shortfall_gbps=0.0, cost_vector={})
    prot_cand = RouteServiceCandidate(
        lever="ip_reroute", reused_lightpaths=("lp-existing",), new_lightpaths=(),
        restored_gbps=100.0, shortfall_gbps=0.0, cost_vector={})
    pair_res = RouteServiceResult(
        status=SolverStatus.PARTIAL, service_id="svc1", demand_gbps=100.0,
        protected=True, candidates=(),
        pairs=(RoutePair(working=work_cand, protection=prot_cand, disjoint=False,
                         shared_assets=("f_omsAB",), shared_groups=(),
                         cost_vector={"scalar": -3.0}),))
    d2 = route_service_result_dict(pair_res)
    assert d2["pairs"][0]["disjoint"] is False
    assert d2["pairs"][0]["shared_assets"] == ["f_omsAB"]
    assert d2["pairs"][0]["cost_vector"] == {"scalar": -3.0}
    # The two legs must not be conflated -- distinct content in each key,
    # matching real route_service's convention of an empty cost_vector per leg.
    working = d2["pairs"][0]["working"]
    protection = d2["pairs"][0]["protection"]
    assert working["lever"] == "optical_reroute"
    assert working["new_lightpaths"][0]["oms_sequence"] == ["omsAB"]
    assert working["reused_lightpaths"] == []
    assert working["cost_vector"] == {}
    assert protection["lever"] == "ip_reroute"
    assert protection["reused_lightpaths"] == ["lp-existing"]
    assert protection["new_lightpaths"] == []
    assert protection["cost_vector"] == {}


def test_route_service_and_evaluate_objective_tools_registered():
    app = build_app()
    _seed(app)
    out = _call(app, "evaluate_objective")
    assert "scalar" in out

    menu = _call(app, "route_service", service_id="svc1")
    assert menu["status"] in ("solution", "partial", "no_solution")
    assert menu["service_id"] == "svc1"


# ---------------------------------------------------------------------------
# Final-review Fix 3: cost_vector (route_service_result_dict's `_cand`/`_pair`
# helpers) is built from route_service._vec, which copies total_margin/scalar
# straight off ObjectiveResult -- the same non-finite-float risk
# objective_result_dict already guards with _safe_float. evaluate_objective's
# total_margin sums EVERY lightpath in the model (not just the candidate's own
# new run), so a pre-existing failed lightpath elsewhere in the model (real
# -inf QoT sentinel via inject_failure, production code) propagates straight
# into every candidate's cost_vector, whether or not that candidate's own
# route touches the failed asset.
# ---------------------------------------------------------------------------


def test_route_service_tool_sanitizes_nonfinite_cost_vector():
    import json
    import math
    from multilayer_optical_mcp.model.assets import Lightpath

    app = build_app()
    n = app._snapshots.current()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))

    # Span A-B: hosts a pre-existing lightpath with no bound IP link/service --
    # contributes to evaluate_objective's total_margin sum via
    # model.list_lightpaths() alone, unrelated to any service being routed.
    add_bidir_span(n, "A", "B", "omsAB")
    mode_id = n.modes.list()[0].id
    n.add_lightpath(Lightpath(id="lp-other", oms_sequence=("omsAB",),
                              mode_id=mode_id, center_freq_hz=193.4e12))

    # Span C-D: the service actually being routed by route_service, entirely
    # disjoint from the failed asset below.
    add_bidir_span(n, "C", "D", "omsCD")
    n.add_router(Router(id="RC", site="C"))
    n.add_router(Router(id="RD", site="D"))
    n.add_service(Service(id="svcCD", src_router="RC", dst_router="RD",
                          demand_gbps=100.0, working_path=()))

    # Real -inf QoT sentinel on lp-other via production inject_failure (not
    # mocked) -- propagates into total_margin/scalar for EVERY candidate
    # route_service scores, including ones that never touch omsAB.
    out = _call(app, "inject_failure", asset_ids=["f_omsAB"])
    assert "lp-other" in out["downed_lightpaths"]

    menu = _call(app, "route_service", service_id="svcCD")
    assert menu["status"] in ("solution", "partial", "no_solution")
    assert menu["candidates"], "expected at least one candidate for svcCD"
    cv = menu["candidates"][0]["cost_vector"]
    assert cv["total_margin"] == "-Infinity"
    assert cv["scalar"] == "Infinity"

    payload = json.dumps(menu)
    reloaded = json.loads(payload)

    def _assert_finite(obj):
        if isinstance(obj, float):
            assert math.isfinite(obj), f"non-finite float leaked through JSON: {obj!r}"
        elif isinstance(obj, dict):
            for v in obj.values():
                _assert_finite(v)
        elif isinstance(obj, list):
            for v in obj:
                _assert_finite(v)

    _assert_finite(reloaded)


def test_evaluate_objective_state_param_reads_the_named_snapshot_not_current():
    app = build_app()
    _seed(app)
    # svc1 is unrouted (working_path=()) -> its 100 Gbps demand is dropped in
    # whatever state we score it against.
    baseline = _call(app, "evaluate_objective")
    assert baseline["dropped_traffic"] == pytest.approx(100.0)

    snap_id = _call(app, "snapshot_create")["id"]

    # Mutate `current()` AFTER the snapshot: add a second unrouted service.
    # The snapshot must not see it -- proving state=<id> resolves via
    # snapshots.get(state), not snapshots.current().
    app._snapshots.current().add_router(Router(id="RC", site="C"))
    app._snapshots.current().add_service(Service(
        id="svc2", src_router="RA", dst_router="RC",
        demand_gbps=40.0, working_path=()))

    current_after = _call(app, "evaluate_objective")
    assert current_after["dropped_traffic"] == pytest.approx(140.0)   # svc1 + svc2

    snapshot_result = _call(app, "evaluate_objective", state=snap_id)
    assert snapshot_result["dropped_traffic"] == pytest.approx(100.0)  # svc1 only
