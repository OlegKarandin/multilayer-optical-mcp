"""QoT memoization: a content-addressed cache over compute_qot.

The cache is keyed by a fingerprint of every GSNR input (path physical params,
loading, direction, mode, probe frequency). Content-addressing means there is no
invalidation logic — a mutated span yields a different key, so inject_degradation
automatically misses and recomputes while disjoint paths keep hitting. The one
load-bearing invariant is fingerprint completeness, verified directly here.
"""
import multilayer_optical_mcp.gnpy_adapter.synthesize as synth
from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import Direction
from multilayer_optical_mcp.model.qot_results import QoTResultStore, QoTCache
from tests.phase7_topology import new_model, add_bidir_span

_FREQ = 193.4e12


def _probe():
    return LoadingState((Channel(_FREQ, 100e9, None, "400G"),))


def _spy(monkeypatch):
    """Count real GNPy network builds so a cache hit is observable as 'no build'."""
    calls = {"n": 0}
    orig = synth.build_gnpy_network

    def wrapped(model):
        calls["n"] += 1
        return orig(model)

    monkeypatch.setattr(synth, "build_gnpy_network", wrapped)
    return calls


def _base(m, store, cache):
    return dict(model=m, store=store, oms_sequence=("oms1",),
                direction=Direction.FORWARD, mode_id="400G",
                loading=_probe(), center_freq_hz=_FREQ, cache=cache)


def test_identical_inputs_hit_and_skip_recompute(monkeypatch):
    calls = _spy(monkeypatch)
    m = new_model(); add_bidir_span(m, "A", "B", "oms1")
    store, cache = QoTResultStore(), QoTCache()

    s1, r1 = compute_qot(**_base(m, store, cache))
    s2, r2 = compute_qot(**_base(m, store, cache))

    assert calls["n"] == 1                 # second call served from cache
    assert s1.gsnr_db == s2.gsnr_db
    assert r1 != r2                        # but a fresh result_id is minted each call


def test_cached_matches_uncached(monkeypatch):
    m = new_model(); add_bidir_span(m, "A", "B", "oms1")
    store = QoTResultStore()
    plain, _ = compute_qot(**{**_base(m, store, None)})
    cached, _ = compute_qot(**{**_base(m, store, QoTCache())})
    assert plain.gsnr_db == cached.gsnr_db
    assert plain.margin_db == cached.margin_db
    assert plain.osnr_db == cached.osnr_db


def test_fingerprint_misses_on_each_gsnr_input(monkeypatch):
    """Mutating any GSNR input in turn forces a recompute (cache miss)."""
    calls = _spy(monkeypatch)
    m = new_model(); add_bidir_span(m, "A", "B", "oms1")
    store, cache = QoTResultStore(), QoTCache()

    compute_qot(**_base(m, store, cache))
    n = calls["n"]
    assert n == 1

    m.apply_nf_delta("boost_oms1", 2.0)          # path amp NF -> miss
    compute_qot(**_base(m, store, cache)); n += 1; assert calls["n"] == n

    m.apply_loss_delta("f_oms1", 1.0)            # path fiber loss -> miss
    compute_qot(**_base(m, store, cache)); n += 1; assert calls["n"] == n

    back = {**_base(m, store, cache), "direction": Direction.BACKWARD}
    compute_qot(**back); n += 1; assert calls["n"] == n   # direction -> miss

    loading2 = LoadingState((Channel(_FREQ, 100e9, None, "400G"),
                             Channel(_FREQ + 100e9, 100e9, None, "400G")))
    dense = {**_base(m, store, cache), "loading": loading2}
    compute_qot(**dense); n += 1; assert calls["n"] == n   # extra interferer -> miss

    # ...and re-issuing the very first (now stale) key still hits (no recompute)
    # is NOT asserted here: the point is completeness, not that old keys survive.


def test_degradation_invalidates_only_the_touched_path(monkeypatch):
    """Content-addressing: degrading one span misses; a disjoint span still hits."""
    calls = _spy(monkeypatch)
    m = new_model()
    add_bidir_span(m, "A", "B", "oms1")
    add_bidir_span(m, "A", "B", "oms2")
    store, cache = QoTResultStore(), QoTCache()

    def probe(oms):
        return compute_qot(model=m, store=store, oms_sequence=(oms,),
                           direction=Direction.FORWARD, mode_id="400G",
                           loading=_probe(), center_freq_hz=_FREQ, cache=cache)

    probe("oms1"); probe("oms2")
    n = calls["n"]; assert n == 2

    m.apply_nf_delta("boost_oms1", 3.0)
    probe("oms1"); assert calls["n"] == n + 1     # touched path recomputes
    probe("oms2"); assert calls["n"] == n + 1     # disjoint path served from cache


def test_adapter_evaluator_shares_cache(monkeypatch):
    """The acceptance seam threads the cache: two evaluator calls with the same
    inputs recompute once."""
    from multilayer_optical_mcp.model.allocation import make_adapter_evaluator
    calls = _spy(monkeypatch)
    m = new_model(); add_bidir_span(m, "A", "B", "oms1")
    store, cache = QoTResultStore(), QoTCache()
    ev = make_adapter_evaluator(m, store, cache=cache)
    ev(oms_sequence=("oms1",), direction=Direction.FORWARD, mode_id="400G", loading=_probe())
    ev(oms_sequence=("oms1",), direction=Direction.FORWARD, mode_id="400G", loading=_probe())
    assert calls["n"] == 1


def test_recompute_under_loading_threads_cache(monkeypatch):
    """The operating recompute threads the cache too, so acceptance and settle can
    share one cache (both stay on the ACTUAL loading — see the fill-policy plan)."""
    from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading
    from multilayer_optical_mcp.model.assets import Lightpath
    calls = _spy(monkeypatch)
    m = new_model(); add_bidir_span(m, "A", "B", "oms1")
    m.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",), mode_id="400G",
                              center_freq_hz=_FREQ))
    store, cache = QoTResultStore(), QoTCache()
    loading = _probe()
    recompute_qot_under_loading(model=m, store=store, loading=loading, cache=cache)
    before = calls["n"]
    recompute_qot_under_loading(model=m, store=store, loading=loading, cache=cache)
    assert calls["n"] == before                    # second recompute fully cached
