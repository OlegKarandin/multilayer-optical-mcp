"""Disjointness must exclude a path's own endpoint ROADMs/nodes.

Two paths between the same endpoints necessarily share those endpoints; that is
the definition of connecting the same pair, not a routing correlation. On an
importer-built topology the source ROADM `roadm_<src>` is `elements[0]` of every
OMS, so working and protection paths (which always share their source) would
otherwise collide on it and false-fire DISJOINTNESS_COLLAPSE. See
exposure.path_endpoint_exclusions / path_basis_keys.
"""
from __future__ import annotations

from multilayer_optical_mcp.model.assets import Lightpath, SRLG, TransceiverMode
from multilayer_optical_mcp.model.ip_assets import IPLink, Service
from multilayer_optical_mcp.model.exposure import path_basis_keys, split_shared_keys
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.solvers import check_disjointness
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.validate import _disjointness_findings, ViolationType


def _importer_two_route_service():
    """Real importer topology: a direct A->B route and a span-disjoint A->C->B
    route, with a protected service whose working rides A->B and protection rides
    A->C->B. The two share only the ingress ROADM roadm_A."""
    modes = ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9),
    ])
    graph = {
        "nodes": [{"id": "A"}, {"id": "B"}, {"id": "C"}],
        "edges": [
            {"src": "A", "dst": "B", "length_km": 80.0},
            {"src": "A", "dst": "C", "length_km": 80.0},
            {"src": "C", "dst": "B", "length_km": 80.0},
        ],
    }
    m = model_from_abstract_graph(graph, modes=modes)
    for lp_id, oms in (("lp_AB", "oms_A_B"), ("lp_AC", "oms_A_C"), ("lp_CB", "oms_C_B")):
        m.add_lightpath(Lightpath(id=lp_id, oms_sequence=(oms,), mode_id="100G-QPSK",
                                  center_freq_hz=193.4e12))
    m.add_ip_link(IPLink(id="ip_AB", a_router="router_A", z_router="router_B",
                         lightpath_id="lp_AB"))
    m.add_ip_link(IPLink(id="ip_AC", a_router="router_A", z_router="router_C",
                         lightpath_id="lp_AC"))
    m.add_ip_link(IPLink(id="ip_CB", a_router="router_C", z_router="router_B",
                         lightpath_id="lp_CB"))
    m.add_service(Service(id="svc", src_router="router_A", dst_router="router_B",
                          demand_gbps=10.0, working_path=("ip_AB",),
                          protection_path=("ip_AC", "ip_CB")))
    return m


def test_importer_span_disjoint_protection_not_flagged_collapse():
    """The production manifestation: a genuinely span-disjoint working/protection
    pair on an importer topology must NOT report DISJOINTNESS_COLLAPSE just
    because both begin at roadm_A."""
    m = _importer_two_route_service()
    findings = _disjointness_findings(m, "physical", "link")
    collapse = [f for f in findings if f[0] == ViolationType.DISJOINTNESS_COLLAPSE]
    assert collapse == [], collapse

    # And the audit primitive agrees: the two OMS-sequences are disjoint.
    res = check_disjointness(m, ("oms_A_B",), ("oms_A_C", "oms_C_B"),
                             basis="physical", level="link")
    assert res.disjoint is True, res.shared_assets


def test_path_basis_keys_excludes_endpoint_roadm():
    m = _importer_two_route_service()
    ka = path_basis_keys(m, ("oms_A_B",), basis="physical", level="link")
    kb = path_basis_keys(m, ("oms_A_C", "oms_C_B"), basis="physical", level="link")
    assert not (ka & kb)                       # only shared asset was the endpoint
    assets_a, _ = split_shared_keys(ka)
    assert "roadm_A" not in assets_a           # ingress ROADM projected out


def test_shared_intermediate_asset_still_collapses():
    """Endpoint exclusion must not mask a real intermediate correlation: a path
    compared with itself shares its interior fiber and is not disjoint."""
    m = _importer_two_route_service()
    res = check_disjointness(m, ("oms_A_B",), ("oms_A_B",),
                             basis="physical", level="link")
    assert res.disjoint is False
    assert "roadm_A" not in res.shared_assets            # endpoint still excluded
    assert any(a.startswith("fiber_A_B") for a in res.shared_assets)


def test_node_level_excludes_endpoint_nodes():
    """A->B and A->C->B share only endpoint nodes A and B; interior node C
    differs, so they are node-disjoint once endpoints are excluded."""
    m = _importer_two_route_service()
    res = check_disjointness(m, ("oms_A_B",), ("oms_A_C", "oms_C_B"),
                             basis="physical", level="node")
    assert res.disjoint is True, res.shared_assets


def test_srlg_over_only_endpoint_roadm_does_not_collapse():
    """All-bases exclusion: an SRLG whose only shared member is the endpoint
    ROADM is unactionable (no reroute avoids a mandated endpoint) and must not
    collapse disjointness."""
    m = _importer_two_route_service()
    m.add_srlg(SRLG(id="srlg-endpoint", asset_ids=("roadm_A",)))
    res = check_disjointness(m, ("oms_A_B",), ("oms_A_C", "oms_C_B"),
                             basis="srlg", level="srlg")
    assert res.disjoint is True
    assert res.shared_groups == ()


def test_srlg_over_intermediate_fibers_collapses():
    """A shared duct covering an interior fiber from each path is a real
    correlation and must still collapse under the srlg basis."""
    m = _importer_two_route_service()
    m.add_srlg(SRLG(id="srlg-duct", asset_ids=("fiber_A_B_0", "fiber_A_C_0")))
    res = check_disjointness(m, ("oms_A_B",), ("oms_A_C", "oms_C_B"),
                             basis="srlg", level="srlg")
    assert res.disjoint is False
    assert res.shared_groups == ("srlg-duct",)


def test_risk_group_over_only_endpoint_roadm_does_not_collapse():
    m = _importer_two_route_service()
    m.define_risk_group(rg_id="rg-endpoint", asset_ids=("roadm_A",))
    res = check_disjointness(m, ("oms_A_B",), ("oms_A_C", "oms_C_B"),
                             basis="risk_group", level="risk_group")
    assert res.disjoint is True
    assert res.shared_groups == ()
