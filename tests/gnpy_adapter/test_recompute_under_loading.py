from multilayer_optical_mcp.model.assets import (
    Lightpath, Router, IPLink,
)
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading
from tests.gnpy_adapter.test_compute_qot import _toy_model


def _model_with_lightpath():
    n = _toy_model()
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms-AZ",),
                              mode_id="400G@7.1dB", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="Z"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    return n


def test_recompute_writes_state_and_returns_result_ids():
    n = _model_with_lightpath(); store = QoTResultStore()
    loading = LoadingState(channels=(
        Channel(193.4e12, 100e9, None, "400G@7.1dB"),
    ))
    results = recompute_qot_under_loading(model=n, store=store, loading=loading)
    state, rid = results["lp1"]
    # Recorded on the model.
    assert n.get_qot_state("lp1") == state
    # Breakdown reachable from the store.
    bd = store.get(rid)
    assert bd.snapshots
    # And capacity derives correctly.
    cap = n.ip_link_capacity_gbps("ip1")
    assert cap == (400.0 if state.mode_feasible else 0.0)


def test_recompute_honors_uncommitted_additive_channel_via_mcp_tool():
    """CLAUDE.md's adapter contract: a loading state is a first-class input, not
    "the current network" -- the make-before-break overlap (old U new, both lit
    on a span, *before* either is committed) must be evaluable without
    provisioning the new channel first. Drive this through the public MCP tool
    (server.py's registered `recompute_qot_under_loading`), not the adapter
    function directly, since that is the surface the audit finding was about:
    calling the tool with old U new where new isn't provisioned silently
    evaluated only old.
    """
    from multilayer_optical_mcp.server import build_app

    n = _model_with_lightpath()  # lp1 committed on oms-AZ @ 193.4 THz
    app = build_app(model=n)

    def _call(name, **kwargs):
        return app._tool_manager._tools[name].fn(**kwargs)

    committed_only = [
        {"center_freq_hz": 193.4e12, "slot_width_hz": 100e9, "mode_id": "400G@7.1dB"},
    ]
    baseline = _call("recompute_qot_under_loading", loading_channels=committed_only)
    baseline_gsnr = baseline["lp1"]["gsnr_db"]
    # NOTE: a single-carrier loading is not actually single-channel by the time
    # it reaches gnpy -- compute_qot's _ensure_min_two_channels injects a
    # same-power dummy 100 GHz above the probe (193.5 THz here) because gnpy's
    # EDFAs require >=2 carriers to propagate. The baseline above therefore
    # already has an implicit interferer at 193.5 THz. To isolate the effect of
    # the genuinely NEW, uncommitted channel below, the "after" loading
    # explicitly includes that same 193.5 THz channel (so it's held constant)
    # plus one additional channel at 193.2 THz that has no committed source
    # anywhere in the model -- the actual make-before-break addition under test.

    # old U new: 193.2 THz is NOT provisioned anywhere in the model --
    # model.list_lightpaths() still returns only lp1.
    with_extra = committed_only + [
        {"center_freq_hz": 193.5e12, "slot_width_hz": 100e9, "mode_id": "400G@7.1dB"},
        {"center_freq_hz": 193.2e12, "slot_width_hz": 100e9, "mode_id": "400G@7.1dB"},
    ]
    after = _call("recompute_qot_under_loading", loading_channels=with_extra)
    after_gsnr = after["lp1"]["gsnr_db"]

    # The extra channel's NLI must show up as a real, non-negligible drop in
    # lp1's GSNR -- not silently dropped because it was never provisioned.
    assert after_gsnr < baseline_gsnr - 1e-6, (
        f"uncommitted make-before-break channel's NLI contribution was not "
        f"reflected in lp1's GSNR: baseline={baseline_gsnr:.6f} dB, "
        f"with_extra={after_gsnr:.6f} dB"
    )
