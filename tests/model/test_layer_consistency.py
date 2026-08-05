# tests/model/test_layer_consistency.py
import pytest
from multilayer_optical_mcp.model.snapshots import SnapshotStore
from multilayer_optical_mcp.model.ip_assets import Service
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model import ip_routing
from tests.model.test_ip_routing import _two_link_model


def _seeded_store():
    n = _two_link_model()
    # 150G A->B on a 200G link: healthy, util 0.75, no congestion.
    n.add_service(Service(id="svc-AB", src_router="R-A", dst_router="R-B",
                          demand_gbps=150.0, working_path=("ipAB",)))
    store = SnapshotStore(initial=n)
    return store


def test_downshift_on_branch_creates_congestion_and_leaves_truth_intact():
    store = _seeded_store()
    base = store.create()

    # Baseline (ground truth): 200G capacity, util 0.75, healthy.
    base_res = ip_routing.simulate_ip_routing(store.get(base))
    base_util = {u.ip_link_id: u for u in base_res.utilizations}["ipAB"]
    assert base_util.capacity_gbps == 200.0
    assert base_util.utilization == pytest.approx(0.75)
    assert base_res.congested_links == ()

    # Branch and downshift 16QAM -> QPSK on the branch only.
    branch = store.branch(base)
    bm = store.current()  # branch() makes this the unfrozen working copy
    bm.set_lightpath_mode("lpAB", "100G-QPSK")
    # Single-transponder-type network => GSNR unchanged; only the mode's
    # required threshold changes. QPSK threshold (12 dB) < achieved 22 dB, so
    # margin stays positive and capacity follows the new mode to 100G.
    bm.set_qot_state("lpAB", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=10.0))

    branch_res = ip_routing.simulate_ip_routing(bm)
    branch_util = {u.ip_link_id: u for u in branch_res.utilizations}["ipAB"]
    assert branch_util.capacity_gbps == 100.0          # halved, derived from mode
    assert branch_util.utilization == pytest.approx(1.5)
    assert branch_res.congested_links == ("ipAB",)     # 150 on 100 -> congested
    assert branch_res.overflow_gbps == pytest.approx(50.0)

    # Ground truth untouched: re-simulate the base snapshot.
    again = ip_routing.simulate_ip_routing(store.get(base))
    assert {u.ip_link_id: u for u in again.utilizations}["ipAB"].capacity_gbps == 200.0
    assert again.congested_links == ()


def test_margin_negative_on_branch_drops_the_service():
    store = _seeded_store()
    base = store.create()
    branch = store.branch(base)
    bm = store.current()  # branch() makes this the unfrozen working copy
    # Degradation pushes lpAB margin negative -> ipAB down -> svc-AB dropped.
    bm.set_qot_state("lpAB", QoTState(gsnr_db=17.0, osnr_db=19.0, margin_db=-1.0))
    res = ip_routing.simulate_ip_routing(bm)
    assert res.down_links == ("ipAB",)
    assert res.dropped_services == (
        ip_routing.DroppedService("svc-AB", "link_down", "ipAB"),
    )
    # Base snapshot still healthy.
    assert ip_routing.simulate_ip_routing(store.get(base)).down_links == ()
