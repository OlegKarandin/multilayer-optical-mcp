"""Integration tests: injection propagates through optical -> IP -> routing layers
on a branch, while leaving ground truth untouched.
"""
import math
from multilayer_optical_mcp.model.assets import IPLink, Lightpath, Service
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.snapshots import SnapshotStore
from multilayer_optical_mcp.model.whatif import (
    inject_failure,
    inject_degradation,
    loading_from_model,
)
from multilayer_optical_mcp.model.ip_routing import simulate_ip_routing, DroppedService
from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading

# reuse the one-edge synthesizable model + helpers from test_whatif
from tests.model.test_whatif import _one_edge_model, _reg, MODE


def _ip_over_optical():
    """One-edge model with a lightpath, two routers, one IP link, one service.

    Service demand (300 Gbps) is below the mode bitrate (400 Gbps), so the link
    is healthy and the service fits when the optical layer is intact.
    """
    m = _one_edge_model()
    m.add_lightpath(Lightpath(
        id="lp0",
        oms_sequence=("oms_0_1",),
        mode_id=MODE,
        center_freq_hz=193.4e12,
    ))
    # router_0/router_1 are already registered by model_from_abstract_graph
    # (via _one_edge_model) -- re-adding them would now raise (duplicate-id
    # guard, Task 1 finding 4).
    m.add_ip_link(IPLink(
        id="ip0",
        a_router="router_0",
        z_router="router_1",
        lightpath_id="lp0",
    ))
    m.add_service(Service(
        id="svc0",
        src_router="router_0",
        dst_router="router_1",
        demand_gbps=300.0,
        working_path=("ip0",),
    ))
    # Seed real QoT so margin is recorded and capacity is non-zero.
    recompute_qot_under_loading(
        model=m,
        store=QoTResultStore(),
        loading=loading_from_model(m),
    )
    return m


# ---------------------------------------------------------------------------
# Test 1: fiber failure -> capacity 0 -> service dropped; ground truth intact
# ---------------------------------------------------------------------------

def test_failure_zeroes_capacity_and_drops_service():
    base = _ip_over_optical()
    store = SnapshotStore(base)
    sid = store.create()
    store.branch(sid)
    branch = store.current()

    # Healthy before injection: positive capacity, service fits.
    assert branch.ip_link_capacity_gbps("ip0") > 0

    # Inject failure of a fiber that crosses lp0's OMS.
    inject_failure(branch, ("fiber_0_1_0",))

    # Optical -> IP: margin is now -inf, so capacity gate fires -> capacity 0.
    assert branch.ip_link_capacity_gbps("ip0") == 0.0

    # IP -> routing: simulate_ip_routing sees a down link and drops the service.
    result = simulate_ip_routing(branch)
    assert "svc0" in {d.service_id for d in result.dropped_services}

    # Ground truth (base snapshot) is untouched.
    assert store.get(sid).ip_link_capacity_gbps("ip0") > 0
    base_result = simulate_ip_routing(store.get(sid))
    assert not any(d.service_id == "svc0" for d in base_result.dropped_services)


# ---------------------------------------------------------------------------
# Test 2: large NF injection can push margin negative -> capacity 0; ground
#         truth margin unchanged.
# ---------------------------------------------------------------------------

def test_nf_injection_can_drop_capacity_to_zero_when_margin_goes_negative():
    base = _ip_over_optical()
    base_margin = base.get_qot_state("lp0").margin_db

    store = SnapshotStore(base)
    sid = store.create()
    store.branch(sid)
    branch = store.current()

    # A +20 dB NF bump should push margin well negative on any realistic span.
    report = inject_degradation(
        branch,
        store=QoTResultStore(),
        asset_id="amp_0_1_0",
        nf_delta=20.0,
    )

    branch_margin = branch.get_qot_state("lp0").margin_db

    if branch_margin < 0:
        # Margin gate: capacity must be 0, lightpath must appear in crossings.
        assert branch.ip_link_capacity_gbps("ip0") == 0.0
        assert "lp0" in report.crossings
        # Routing: service is dropped because the link is down.
        result = simulate_ip_routing(branch)
        assert "svc0" in {d.service_id for d in result.dropped_services}
    else:
        # If somehow still feasible, at least confirm margin was lowered.
        assert branch_margin < base_margin

    # Ground truth: the base snapshot's margin is unchanged.
    assert store.get(sid).get_qot_state("lp0").margin_db == base_margin
