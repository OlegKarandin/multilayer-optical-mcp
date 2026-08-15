from multilayer_optical_network.model.assets import Lightpath
from multilayer_optical_network.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_network.gnpy_adapter.adapter import _ensure_min_two_channels
from multilayer_optical_mcp.server import build_app
from multilayer_optical_network.testing import MODE, ROUTE1, ROUTE2, _diamond_model, _model_with_lightpath
from tests.conftest import call_tool


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
    n = _model_with_lightpath()  # lp1 committed on oms-AZ @ 193.4 THz
    app = build_app(model=n)

    committed_only = [
        {"center_freq_hz": 193.4e12, "slot_width_hz": 100e9, "mode_id": "400G@7.1dB"},
    ]
    baseline = call_tool(app, "recompute_qot_under_loading", loading_channels=committed_only)
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

    # Pin the dependency the NOTE above relies on: a single committed channel
    # at 193.4 THz gets a dummy injected at probe + 100 GHz = 193.5 THz (see
    # _ensure_min_two_channels). If that offset ever changes, this assertion
    # fails loudly instead of this test silently measuring a different
    # scenario than the NOTE claims.
    dummy_check = _ensure_min_two_channels(
        LoadingState(channels=(Channel(193.4e12, 100e9, None, "400G@7.1dB"),)),
        probe_freq_hz=193.4e12,
    )
    assert {c.center_freq_hz for c in dummy_check.channels} == {193.4e12, 193.5e12}

    # old U new: 193.2 THz is NOT provisioned anywhere in the model --
    # model.list_lightpaths() still returns only lp1.
    with_extra = committed_only + [
        {"center_freq_hz": 193.5e12, "slot_width_hz": 100e9, "mode_id": "400G@7.1dB"},
        {"center_freq_hz": 193.2e12, "slot_width_hz": 100e9, "mode_id": "400G@7.1dB"},
    ]
    after = call_tool(app, "recompute_qot_under_loading", loading_channels=with_extra)
    after_gsnr = after["lp1"]["gsnr_db"]

    # The extra channel's NLI must show up as a real, non-negligible drop in
    # lp1's GSNR -- not silently dropped because it was never provisioned.
    assert after_gsnr < baseline_gsnr - 1e-6, (
        f"uncommitted make-before-break channel's NLI contribution was not "
        f"reflected in lp1's GSNR: baseline={baseline_gsnr:.6f} dB, "
        f"with_extra={after_gsnr:.6f} dB"
    )
    # The broadcast is also surfaced to the caller: neither 193.5 THz (the held-
    # constant dummy-matching channel) nor 193.2 THz (the actual make-before-
    # break addition) has a committed source anywhere in the model -- lp1 is
    # the only committed lightpath, at 193.4 THz -- so both appear in the meta
    # field. This is the transparency half of the fix (Important-2).
    assert after["_broadcast_to_all_lightpaths_hz"] == [193.2e12, 193.5e12]
    # And the baseline call (only the committed 193.4 THz channel) reports none.
    assert baseline["_broadcast_to_all_lightpaths_hz"] == []


def test_recompute_does_not_resolve_same_frequency_reroute():
    """Regression-lock for Important-1: a same-frequency reroute is NOT
    resolved by recompute_qot_under_loading, and this is a documented,
    current limitation (see the function's docstring in adapter.py), not a
    bug to be silently fixed later without updating this test.

    Setup: two node-disjoint OMS routes (the same diamond fixture used by
    S4-6/S8-5's per-path-comb tests) each carry one committed lightpath --
    lp1 on ROUTE1 @ F, lp2 on ROUTE2 @ G. Imagine lp1 is about to be torn
    down and replaced by a NEW lightpath on ROUTE2 that happens to reuse F
    (a same-frequency reroute/make-before-break transient). A caller
    constructs loading = {lp1@F, lp2@G, "new channel"@F} intending the new
    channel to land on ROUTE2 as an interferer for lp2.

    Because F is still committed (lp1 hasn't been torn down), F is in
    `known`, so the slot is classified "committed, scope to ROUTE1" rather
    than "uncommitted, broadcast everywhere" -- ROUTE2's lp2 never sees it.
    lp2's GSNR under this loading must be IDENTICAL to lp2's GSNR under the
    loading that omits the "new" channel entirely -- proving the intended
    new channel contributed nothing, silently.
    """
    F = 193.4e12  # lp1's frequency on ROUTE1; also the "new" channel's frequency
    G = 193.7e12  # lp2's frequency on ROUTE2; kept clear of any dummy injection

    n = _diamond_model()
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=ROUTE1,
                              mode_id=MODE, center_freq_hz=F))
    n.add_lightpath(Lightpath(id="lp2", oms_sequence=ROUTE2,
                              mode_id=MODE, center_freq_hz=G))
    app = build_app(model=n)

    committed_only = [
        {"center_freq_hz": F, "slot_width_hz": 100e9, "mode_id": MODE},
        {"center_freq_hz": G, "slot_width_hz": 100e9, "mode_id": MODE},
    ]
    baseline = call_tool(app, "recompute_qot_under_loading", loading_channels=committed_only)
    baseline_lp2_gsnr = baseline["lp2"]["gsnr_db"]
    assert baseline["_broadcast_to_all_lightpaths_hz"] == []

    # "old U new" for a same-frequency reroute: lp1 still committed (not torn
    # down yet) at F, PLUS the caller's intended new channel at F -- which the
    # caller means for ROUTE2, but nothing in Channel says so.
    attempted_reroute = committed_only + [
        {"center_freq_hz": F, "slot_width_hz": 100e9, "mode_id": MODE},
    ]
    after = call_tool(app, "recompute_qot_under_loading", loading_channels=attempted_reroute)
    after_lp2_gsnr = after["lp2"]["gsnr_db"]

    # The known limitation: F is already in `known` (lp1 still holds it on
    # ROUTE1), so it is NOT broadcast -- the "new" channel is invisible.
    assert after["_broadcast_to_all_lightpaths_hz"] == []
    assert after_lp2_gsnr == baseline_lp2_gsnr, (
        "documented limitation regressed: a same-frequency reroute's intended "
        "new channel must currently be silently invisible to the destination "
        "OMS's lightpaths (no OMS/path info on Channel to scope it) -- if this "
        "now changes lp2's GSNR, either the limitation was fixed (update this "
        "test's docstring and assertions) or a real bug was introduced"
    )
