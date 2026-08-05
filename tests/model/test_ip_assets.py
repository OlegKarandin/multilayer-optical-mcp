from multilayer_optical_mcp.model.ip_assets import IPLink


def test_ip_link_bound_to_lightpath_no_capacity_field():
    link = IPLink(id="ip-1", a_router="R1", z_router="R2", lightpath_id="lp1")
    assert not hasattr(link, "capacity_gbps")
