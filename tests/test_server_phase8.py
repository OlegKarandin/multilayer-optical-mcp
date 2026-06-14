# tests/test_server_phase8.py
"""compute_restoration MCP tool returns a structured candidate list."""
from multilayer_optical_mcp.model.restoration import (
    RestorationResult, RestorationCandidate,
)
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.multilayer_graph import NewLightpathRun
from multilayer_optical_mcp.model.views import restoration_result_dict


def test_restoration_result_dict_shape():
    res = RestorationResult(
        status=SolverStatus.PARTIAL, service_id="svc", demand_gbps=50.0,
        candidates=(
            RestorationCandidate(
                lever="ip_reroute", reused_lightpaths=("lp-AM", "lp-MB"),
                new_lightpaths=(), restored_gbps=20.0, shortfall_gbps=30.0,
                cost_facets={"transponders": 0.0, "new_lightpaths": 0.0, "hops": 2.0}),
            RestorationCandidate(
                lever="optical_reroute", reused_lightpaths=(),
                new_lightpaths=(NewLightpathRun(("oms-AB",), 0, "100G", 15.0, 100.0),),
                restored_gbps=50.0, shortfall_gbps=0.0,
                cost_facets={"transponders": 2.0, "new_lightpaths": 1.0, "hops": 1.0}),
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
